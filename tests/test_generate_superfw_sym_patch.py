import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest


RUNNER_PATH = Path(__file__).resolve().parents[1] / "SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.py"
sys.path.insert(0, str(RUNNER_PATH.parent))
SPEC = importlib.util.spec_from_file_location("superfw_patch_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FakeGamePatch:
    def __init__(self, gamecode, gamever, targets, romsize):
        self.save_type = 2

    def waitcnt_patches(self):
        return [0x11223344]

    def save_patches(self):
        return [0x22334455]

    def irq_patches(self):
        return [0x33445566]

    def rtc_patches(self):
        return [0x44556677, 0x44556678, 0x44556679, 0x4455667A]

    def layout_patches(self):
        return [0x55667788]


class FakeGenerator:
    GamePatch = FakeGamePatch
    PROGRAMS = [b"A", b"BC", b"", b"D" * 60]

    @staticmethod
    def gen_cpyhalfword(addr, halfw):
        return [(3 << 28) | (1 << 25) | addr, halfw]

    @staticmethod
    def gen_cpywords(addr, words):
        return [(4 << 28) | ((len(words) - 1) << 25) | addr] + words


class HighRomGamePatch(FakeGamePatch):
    def layout_patches(self):
        return [(0x36B0 << 16) | 0x4DF]


class HighRomGenerator(FakeGenerator):
    GamePatch = HighRomGamePatch


HIGH_TAIL_LAYOUT_WORD = (0x7AC3 << 16) | 0x53C


class HighTailGamePatch(FakeGamePatch):
    def layout_patches(self):
        return [HIGH_TAIL_LAYOUT_WORD]


class HighTailGenerator(FakeGenerator):
    GamePatch = HighTailGamePatch


EXPECTED_0212_HANDLER = (
    0x4778B530, 0xE10F3000, 0xE321F09B, 0xE20D1007, 0xE04DD181, 0x328EE001, 0x328DDD16, 0xE1A0500E,
    0xE121F003, 0xE28F2001, 0xE12FFF12, 0x213C1C04, 0xF82EF000, 0x213C71A0, 0xF82AF000, 0x21187160,
    0xF826F000, 0x1C287120, 0x21073006, 0x70E1DF06, 0x71E22240, 0x300120FF, 0x31B921B4, 0x0783A211,
    0x3101D101, 0x1A6D320C, 0x186DD2F5, 0xF814F000, 0x20007020, 0x30015C11, 0xD2FB1A6D, 0xF000186D,
    0x7060F80B, 0x30011C28, 0xF806F000, 0xBD3070A0, 0xDF061C28, 0x1C081C05, 0xDF06210A, 0x43080100,
    0x46C04770, 0x1E1F1C1F, 0x1F1F1E1F, 0x1F1E1F1E, 0x1E1F1D1F, 0x1F1F1E1F, 0x1F1E1F1E,
)


def copy_operations(words, start):
    copies = []
    cursor = start
    while cursor < len(words):
        operation = words[cursor]
        word_count = ((operation >> 25) & 7) + 1
        copies.append((operation >> 28, operation & 0x1FFFFFF, words[cursor + 1:cursor + 1 + word_count]))
        cursor += word_count + 1
    return copies


def result(targets=None):
    return {
        "result": "ok",
        "data": {
            "game-code": "BPRJ",
            "game-version": 1,
            "filesize": 0x200,
            "targets": targets or {},
        },
    }


class PipelineTests(unittest.TestCase):
    def test_ptype_order_swi1_append_and_symmap_rtc_precedence(self):
        rtc_detected = {"probe_fn": {"addr": "0x10"}}
        rtc_from_sym = {"probe_fn": {"addr": "0x20"}}
        results = {
            "waitcnt": result({"waitcnt": {"patch-sites": [{"id": "wait"}]}}),
            "irq": result({"irqhdr": {"patch-sites": []}}),
            "swi1": result({"waitcnt": {"patch-sites": [{"id": "swi"}]}}),
            "save": result({"sram": {}}),
            "layout": result({"layout": {"info": {}}}),
            "rtc": result({"rtc": rtc_detected}),
            "symmap": result({"rtc": rtc_from_sym}),
        }

        patchset = runner.merge_results(results)

        self.assertEqual(
            patchset["targets"]["waitcnt"]["patch-sites"],
            [{"id": "wait"}, {"id": "swi"}],
        )
        self.assertEqual(patchset["targets"]["rtc"], rtc_from_sym)
        self.assertNotIn("swi1", patchset["targets"])
        self.assertEqual(patchset["files"], [])
        self.assertEqual(patchset["romsize"], 0x200)

    def test_all_analyzers_run_in_web_order_and_receive_sym(self):
        seen = []

        class Analyzer:
            def __init__(self, name):
                self.name = name

            def process_rom(self, rom, **kwargs):
                seen.append((self.name, rom, kwargs["sym"]))
                if self.name in ("irq", "swi1", "save", "rtc"):
                    return None
                target = {"waitcnt": {"patch-sites": []}} if self.name == "waitcnt" else {}
                if self.name == "layout":
                    target = {"layout": {"info": {}}}
                if self.name == "symmap":
                    target = {"rtc": {"probe_fn": {"addr": "0x10"}}}
                return {"game-code": "BPRJ", "game-version": 0, "filesize": len(rom), "targets": target}

        analyzers = {name: Analyzer(name) for name in runner.PTYPES}
        patchset = runner.analyze_rom(b"synthetic", "synthetic symbols", analyzers=analyzers)
        self.assertEqual([item[0] for item in seen], list(runner.PTYPES))
        self.assertTrue(all(item[1:] == (b"synthetic", "synthetic symbols") for item in seen))
        self.assertEqual(patchset["targets"]["rtc"]["probe_fn"]["addr"], "0x10")

    def test_analyzer_error_fails_closed(self):
        class Analyzer:
            def __init__(self, name):
                self.name = name

            def process_rom(self, rom, **kwargs):
                if self.name == "irq":
                    raise RuntimeError("synthetic failure")
                return None

        analyzers = {name: Analyzer(name) for name in runner.PTYPES}
        with self.assertRaisesRegex(runner.GenerationError, "Analyzer irq failed"):
            runner.analyze_rom(b"synthetic", "symbols", analyzers=analyzers)

    def test_missing_symmap_rtc_fails_closed(self):
        results = {ptype: result() for ptype in runner.PTYPES}
        results["waitcnt"] = result({"waitcnt": {"patch-sites": []}})
        results["swi1"] = {"result": "ok", "data": None}
        results["layout"] = result({"layout": {"info": {}}})
        with self.assertRaisesRegex(runner.GenerationError, "did not provide RTC"):
            runner.merge_results(results)

    def test_vendored_analyzers_build_patch_from_synthetic_inputs(self):
        rom = bytearray(0x200)
        rom[0xAC:0xB0] = b"BPRJ"
        rom[0xBC] = 0
        symbols = "\n".join([
            "080000d0 g 00000004 SiiRtcProbe",
            "080000d4 g 00000004 SiiRtcReset",
            "080000d8 g 00000004 SiiRtcGetStatus",
            "080000dc g 000000bc SiiRtcGetDateTime",
        ])

        patch, counts = runner.build_patch(bytes(rom), symbols, profile="legacy-v0.19-v0.21")

        self.assertEqual(len(patch), 800)
        self.assertEqual(counts["rtc"], 14)
        import patchtool.generator
        rtc_offset = 288 + 4 * (counts["waitcnt"] + counts["save"] + counts["irq"])
        rtc_words = struct.unpack_from("<14I", patch, rtc_offset)
        self.assertEqual(rtc_words[:4], (
            patchtool.generator.gen_rtc_opc(0xD0, patchtool.generator.RTC_PROBE_HNDLR)[0],
            patchtool.generator.gen_rtc_opc(0xD4, patchtool.generator.RTC_RESET_HNDLR)[0],
            patchtool.generator.gen_rtc_opc(0xD8, patchtool.generator.RTC_STSRD_HNDLR)[0],
            patchtool.generator.gen_rtc_opc(0xDC, patchtool.generator.RTC_GETTD_HNDLR)[0],
        ))
        self.assertEqual(rtc_words[4:6], tuple(patchtool.generator.gen_cpyhalfword(0xDC + 0x68, 0x1C68)))
        self.assertEqual(rtc_words[6:8], tuple(patchtool.generator.gen_cpywords(0xDC + 0x3A, [0xF831F000])))
        self.assertEqual(rtc_words[6] & 0x1FFFFFF, 0xDC + 0x3A)
        self.assertEqual(rtc_words[7], 0xF831F000)
        tail = [0x30061C28, 0xDF062107, 0x224070E1, 0x200071E2, 0x477021B4]
        self.assertEqual(rtc_words[8:], tuple(patchtool.generator.gen_cpywords(0xDC + 0xA0, tail)))
        self.assertEqual(rtc_words[8] & 0x1FFFFFF, 0xDC + 0xA0)

    def test_cli_reports_hashes_and_does_not_publish_after_analysis_failure(self):
        rom = bytearray(0x200)
        rom[0xAC:0xB0] = b"BPRJ"
        rom[0xBC] = 0
        sym_text = "\n".join([
            "080000d0 g 00000004 SiiRtcProbe",
            "080000d4 g 00000004 SiiRtcReset",
            "080000d8 g 00000004 SiiRtcGetStatus",
            "080000dc g 000000bc SiiRtcGetDateTime",
        ])
        with tempfile.TemporaryDirectory() as directory:
            rom_path = Path(directory) / "synthetic.gba"
            sym_path = Path(directory) / "synthetic.sym"
            patch_path = Path(directory) / "synthetic.patch"
            rom_path.write_bytes(rom)
            sym_path.write_text(sym_text, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = runner.main(["--rom", str(rom_path), "--sym", str(sym_path), "--output", str(patch_path)])
            self.assertEqual(status, 0)
            self.assertEqual(patch_path.stat().st_size, 800)
            self.assertEqual(rom_path.read_bytes(), bytes(rom))
            self.assertIn(hashlib.sha256(rom).hexdigest(), stdout.getvalue())
            self.assertIn(hashlib.sha256(sym_text.encode("utf-8")).hexdigest(), stdout.getvalue())
            self.assertIn("PATCH SHA-256:", stdout.getvalue())
            self.assertIn("RTC profile: 0.21.2", stdout.getvalue())
            self.assertIn("waitcnt=0 save=0 save_type=0 irq=0 rtc=57 layout=0", stdout.getvalue())

            patch_path.unlink()
            sym_path.write_text("", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = runner.main(["--rom", str(rom_path), "--sym", str(sym_path), "--output", str(patch_path)])
            self.assertEqual(status, 1)
            self.assertFalse(patch_path.exists())
            self.assertIn("did not provide RTC", stderr.getvalue())


class SerializationTests(unittest.TestCase):
    def test_default_low_rom_writes_exact_0212_handler_in_chunked_copies(self):
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {"rtc": {"gettimedate_fn": {"addr": "0x1234", "size": 188}}},
                "romsize": 0x200,
            },
            generator_module=FakeGenerator,
        )

        self.assertEqual(runner.RTC_GETTIMEDATE_0212_HANDLER_WORDS, EXPECTED_0212_HANDLER)
        self.assertEqual(len(EXPECTED_0212_HANDLER) * 4, 188)
        self.assertEqual(counts["rtc"], 57)
        rtc_words = struct.unpack_from("<57I", patch, 288 + 4 * 3)
        copies = copy_operations(rtc_words, 4)
        self.assertEqual([len(words) for _, _, words in copies], [8, 8, 8, 8, 8, 7])
        self.assertEqual([operation for operation, _, _ in copies], [4] * 6)
        self.assertEqual([address for _, address, _ in copies], [0x1234 + index * 32 for index in range(6)])
        self.assertEqual(tuple(word for _, _, words in copies for word in words), EXPECTED_0212_HANDLER)
        self.assertEqual(copies[-1][2], EXPECTED_0212_HANDLER[40:])

    def test_default_high_rom_relocates_exact_0212_handler_and_veneers_entry(self):
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {
                    "rtc": {"gettimedate_fn": {"addr": "0x10e9bd8", "size": 8}},
                    "layout": {"info": {
                        "tail-padding": 0x14F5EF,
                        "holes": [
                            (0x00DAA180, 0x0013BC90),
                            (0x01597A5B, 0x000A85A5),
                            (0x00B583C4, 0x000A7C3C),
                            (0x00F00000, 0x00039701),
                        ],
                    }},
                },
                "romsize": 0x2000000,
            },
            generator_module=HighTailGenerator,
        )

        self.assertEqual(counts["rtc"], 60)
        self.assertEqual(struct.unpack_from("<I", patch, 16 + 6)[0], (0x36B0 << 16) | 0x4DE)
        rtc_words = struct.unpack_from("<60I", patch, 288 + 4 * 3)
        copies = copy_operations(rtc_words, 4)
        self.assertEqual([len(words) for _, _, words in copies], [8, 8, 8, 8, 8, 7, 2])
        self.assertEqual([operation for operation, _, _ in copies], [4] * 7)
        self.assertEqual([address for _, address, _ in copies[:6]], [0xEE3800 + index * 32 for index in range(6)])
        self.assertEqual(tuple(word for _, _, words in copies[:6] for word in words), EXPECTED_0212_HANDLER)
        self.assertEqual(copies[-1], (4, 0x10E9BD8, (0x47184B00, 0x08EE3801)))

    def test_default_high_rom_replaces_full_0212_handler_in_place(self):
        address = 0x010F03C8
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {"rtc": {"gettimedate_fn": {"addr": hex(address), "size": 188}}},
                "romsize": 0x2000000,
            },
            generator_module=HighTailGenerator,
        )

        self.assertEqual(counts["rtc"], 57)
        self.assertEqual(struct.unpack_from("<I", patch, 16 + 6)[0], HIGH_TAIL_LAYOUT_WORD)
        rtc_words = struct.unpack_from("<57I", patch, 288 + 4 * 3)
        copies = copy_operations(rtc_words, 4)
        self.assertEqual([len(words) for _, _, words in copies], [8, 8, 8, 8, 8, 7])
        self.assertEqual([operation for operation, _, _ in copies], [4] * 6)
        self.assertEqual([target for _, target, _ in copies], [address + index * 32 for index in range(6)])
        self.assertEqual(tuple(word for _, _, words in copies for word in words), EXPECTED_0212_HANDLER)
        self.assertNotIn(0xEE3800, [target for _, target, _ in copies])

    def test_default_profile_rejects_short_target_and_unknown_profile(self):
        for address, size in (
            ("0x1234", 187), ("0x10e9bd8", 7),
            ("0x1235", 188), ("0x1ffff80", 188), ("0x1ffffffc", 8),
        ):
            with self.subTest(address=address, size=size):
                with self.assertRaises(runner.GenerationError):
                    runner.serialize_patch(
                        {
                            "game-code": "BPRJ", "game-version": 1,
                            "targets": {"rtc": {"gettimedate_fn": {"addr": address, "size": size}}},
                            "romsize": 0x2000000,
                        },
                        generator_module=HighRomGenerator,
                    )
        with self.assertRaisesRegex(runner.GenerationError, "Unknown RTC profile"):
            runner.serialize_patch({}, generator_module=FakeGenerator, profile="unsupported")
        with self.assertRaisesRegex(runner.GenerationError, "Unknown RTC profile"):
            runner.build_patch(b"", "", profile="unsupported")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                runner.main([
                    "--rom", "rom.gba", "--sym", "rom.sym", "--output", "rom.patch",
                    "--profile", "unsupported",
                ])
        self.assertEqual(error.exception.code, 2)

    def test_default_high_rom_rejects_overlap(self):
        overlapping = {
            "game-code": "BPRJ", "game-version": 1,
            "targets": {"rtc": {
                "gettimedate_fn": {"addr": "0x10e9bd8", "size": 8},
                "probe_fn": {"addr": "0xee3800", "size": 4},
            }},
            "romsize": 0x2000000,
        }
        with self.assertRaisesRegex(runner.GenerationError, "overlaps"):
            runner.serialize_patch(overlapping, generator_module=HighRomGenerator)

    def test_high_only_layout_is_rejected_for_relocation(self):
        class HighOnlyGamePatch(FakeGamePatch):
            def layout_patches(self):
                return [HIGH_TAIL_LAYOUT_WORD]

        class HighOnlyGenerator(FakeGenerator):
            GamePatch = HighOnlyGamePatch

        with self.assertRaisesRegex(runner.GenerationError, "eligible layout candidate below 16 MiB"):
            runner.serialize_patch(
                {
                    "game-code": "BPRJ", "game-version": 1,
                    "targets": {
                        "rtc": {"gettimedate_fn": {"addr": "0x10e9bd8", "size": 8}},
                        "layout": {"info": {
                            "tail-padding": 0x14F5EF,
                            "holes": [(0x01597A5B, 0x000A85A5)],
                        }},
                    },
                    "romsize": 0x2000000,
                },
                generator_module=HighOnlyGenerator,
            )

    def test_layout_selection_skips_larger_ineligible_holes(self):
        layout = [HIGH_TAIL_LAYOUT_WORD]
        patchset = {
            "romsize": 0x2000000,
            "targets": {"layout": {"info": {
                "tail-padding": 0x14F5EF,
                "holes": [
                    (0x00DAA180, 0x0013BC90),
                    (0x01500000, 0x00200000),
                ],
            }}},
        }

        runner._select_low_rom_layout(patchset, layout)

        self.assertEqual(layout, [(0x36B0 << 16) | 0x4DF])

    def test_serialization_matches_800_byte_header_programs_and_payload(self):
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {"rtc": {"gettimedate_fn": {"addr": "0x1234", "size": 0xB4}}},
                "romsize": 0x200,
            },
            generator_module=FakeGenerator,
            profile="legacy-v0.19-v0.21",
        )
        self.assertEqual(len(patch), 800)
        self.assertEqual(patch[:16], b"SUPERFWPATCHV01\x00")
        self.assertEqual(struct.unpack_from("<BBBBBxIxxxxxx", patch, 16), (1, 1, 2, 1, 14, 0x55667788))
        offset = 32
        for program in FakeGenerator.PROGRAMS:
            length = struct.unpack_from("<I", patch, offset)[0]
            self.assertEqual(length, len(program))
            self.assertEqual(patch[offset + 4:offset + 4 + length], program)
            self.assertEqual(patch[offset + 4 + length:offset + 64], bytes(60 - length))
            offset += 64
        words = struct.unpack_from("<17I", patch, 288)
        self.assertEqual(words[:7], (0x11223344, 0x22334455, 0x33445566, 0x44556677, 0x44556678, 0x44556679, 0x4455667A))
        self.assertEqual(words[7:9], ((3 << 28) | (1 << 25) | 0x129C, 0x1C68))
        self.assertEqual(words[9:11], ((4 << 28) | 0x126E, 0xF831F000))
        self.assertEqual(words[11:], ((4 << 28) | (4 << 25) | 0x12D4, 0x30061C28, 0xDF062107, 0x224070E1, 0x200071E2, 0x477021B4))
        self.assertEqual(patch[356:], bytes(444))
        self.assertEqual(counts, {"waitcnt": 1, "save": 1, "save_type": 2, "irq": 1, "rtc": 14, "layout": 1})

    def test_high_rom_selects_largest_eligible_low_hole_and_relocates_handler(self):
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {
                    "rtc": {"gettimedate_fn": {"addr": "0x10e9bd8", "size": 0xBC}},
                    "layout": {"info": {
                        "tail-padding": 0x14F5EF,
                        "holes": [
                            (0x00DAA180, 0x0013BC90),
                            (0x01597A5B, 0x000A85A5),
                            (0x00B583C4, 0x000A7C3C),
                            (0x00F00000, 0x00039701),
                        ],
                    }},
                },
                "romsize": 0x2000000,
            },
            generator_module=HighTailGenerator,
            profile="legacy-v0.19-v0.21",
        )

        self.assertEqual(len(patch), 800)
        self.assertEqual(struct.unpack_from("<BBBBBxIxxxxxx", patch, 16), (1, 1, 2, 1, 63, (0x36B0 << 16) | 0x4DE))
        rtc_offset = 288 + 4 * 3
        rtc_words = struct.unpack_from("<63I", patch, rtc_offset)
        self.assertEqual(rtc_words[:4], tuple(FakeGamePatch("BPRJ", 1, {}, 0).rtc_patches()))

        copies = []
        cursor = 4
        while cursor < len(rtc_words):
            operation = rtc_words[cursor]
            word_count = ((operation >> 25) & 7) + 1
            copies.append((operation >> 28, operation & 0x1FFFFFF, rtc_words[cursor + 1:cursor + 1 + word_count]))
            cursor += word_count + 1
        self.assertEqual([len(words) for _, _, words in copies], [8, 8, 8, 8, 8, 8, 1, 2])
        self.assertEqual([address for _, address, _ in copies[:7]], [0xEE3800 + index * 0x20 for index in range(7)])
        self.assertTrue(all(operation == 4 for operation, _, _ in copies))
        expected_handler = (
            0x1C04B5F0, 0x46C04778, 0xE10F3000, 0xE321F09B, 0xE08EE18D, 0xE1A0500E, 0xE121F003, 0xE28F2001,
            0xE12FFF12, 0xF000213C, 0x71A0F838, 0xF000213C, 0x7160F834, 0xF0002118, 0x7120F830, 0x30061C28,
            0xDF062107, 0x204070E1, 0x260071E0, 0x31B921B4, 0x07801C30, 0x3101D100, 0xD302428D, 0x36011A6D,
            0xA712E7F4, 0x07801C30, 0x370CD100, 0xF0001C30, 0x7020F818, 0x5DB92600, 0xD302428D, 0x36011A6D,
            0x3601E7F9, 0xF0001C30, 0x7060F80C, 0x1C283501, 0xF807F000, 0x200170A0, 0x1C28BDF0, 0x1C05DF06,
            0x210A1C08, 0x0100DF06, 0x47704308, 0x1E1F1C1F, 0x1F1F1E1F, 0x1F1E1F1E, 0x1E1F1D1F, 0x1F1F1E1F,
            0x1F1E1F1E
        )
        self.assertEqual(tuple(word for _, _, words in copies[:7] for word in words), expected_handler)
        self.assertEqual(len(expected_handler), 49)
        self.assertEqual(copies[-1], (4, 0x10E9BD8, (0x47184B00, 0x08EE3801)))
        self.assertEqual(counts["rtc"], 63)

    def test_high_rom_requires_full_original_gettimedate_span(self):
        for size in (8, 0xB3):
            with self.subTest(size=size):
                with self.assertRaisesRegex(runner.GenerationError, "through offset 0xB3"):
                    runner.serialize_patch(
                        {
                            "game-code": "BPRJ", "game-version": 1,
                            "targets": {"rtc": {"gettimedate_fn": {"addr": "0x10e9bd8", "size": size}}},
                            "romsize": 0x2000000,
                        },
                        generator_module=HighRomGenerator,
                        profile="legacy-v0.19-v0.21",
                    )

    def test_high_rom_relocation_rejects_invalid_layout_and_target(self):
        invalid_cases = [
            ("missing layout", HighRomGamePatch, "0x10e9bd8", None),
            ("malformed layout", HighRomGamePatch, "0x10e9bd8", True),
            ("small hole", HighRomGamePatch, "0x10e9bd8", (0x36B0 << 16) | 1),
            ("unaligned target", HighRomGamePatch, "0x10e9bd9", (0x36B0 << 16) | 0x4DF),
        ]
        for name, base_game_patch, address, layout_word in invalid_cases:
            with self.subTest(name=name):
                layout_patches = lambda self: [] if layout_word is None else [layout_word]
                InvalidLayoutGamePatch = type(
                    "InvalidLayoutGamePatch", (base_game_patch,), {"layout_patches": layout_patches}
                )

                class InvalidLayoutGenerator(FakeGenerator):
                    GamePatch = InvalidLayoutGamePatch

                with self.assertRaises(runner.GenerationError):
                    runner.serialize_patch(
                        {
                            "game-code": "BPRJ", "game-version": 1,
                            "targets": {"rtc": {"gettimedate_fn": {"addr": address, "size": 0xBC}}},
                            "romsize": 0x2000000,
                        },
                        generator_module=InvalidLayoutGenerator,
                        profile="legacy-v0.19-v0.21",
                    )

    def test_relocated_handler_end_crossing_16_mib_is_rejected(self):
        with self.assertRaisesRegex(runner.GenerationError, "below 16 MiB"):
            runner._rtc_relocation_patch(
                {"targets": {"rtc": {}}},
                [(0x3FFF << 16) | 4],
                0x10E9BD8,
                8,
                FakeGenerator,
                [0] * 257,
                8,
            )

    def test_high_rom_relocation_rejects_overlap_and_oversized_payload(self):
        overlapping = {
            "game-code": "BPRJ", "game-version": 1,
            "targets": {"rtc": {
                "gettimedate_fn": {"addr": "0x10e9bd8", "size": 0xBC},
                "probe_fn": {"addr": "0xee3800", "size": 4},
            }},
            "romsize": 0x2000000,
        }
        with self.assertRaisesRegex(runner.GenerationError, "overlaps"):
            runner.serialize_patch(overlapping, generator_module=HighRomGenerator, profile="legacy-v0.19-v0.21")

        class LargePayloadGamePatch(HighRomGamePatch):
            def waitcnt_patches(self):
                return [0x11223344] * 130

        class LargePayloadGenerator(HighRomGenerator):
            GamePatch = LargePayloadGamePatch

        with self.assertRaisesRegex(runner.GenerationError, "exceeds 512 bytes"):
            runner.serialize_patch(
                {
                    "game-code": "BPRJ", "game-version": 1,
                    "targets": {"rtc": {"gettimedate_fn": {"addr": "0x10e9bd8", "size": 0xBC}}},
                    "romsize": 0x2000000,
                },
                generator_module=LargePayloadGenerator,
                profile="legacy-v0.19-v0.21",
            )

    def test_missing_malformed_or_too_small_gettimedate_target_fails_closed(self):
        targets = [
            {},
            {"rtc": {"gettimedate_fn": {"addr": "not-hex", "size": 0x80}}},
            {"rtc": {"gettimedate_fn": {"addr": "0x1234", "size": 0xB3}}},
        ]
        for invalid_targets in targets:
            with self.subTest(targets=invalid_targets):
                with self.assertRaisesRegex(runner.GenerationError, "gettimedate target"):
                    runner.serialize_patch(
                        {"game-code": "BPRJ", "game-version": 1, "targets": invalid_targets, "romsize": 0x200},
                        generator_module=FakeGenerator,
                        profile="legacy-v0.19-v0.21",
                    )

    def test_gettimedate_tail_address_end_overflow_fails_closed(self):
        with self.assertRaisesRegex(runner.GenerationError, "gettimedate target"):
            runner.serialize_patch(
                {
                    "game-code": "BPRJ", "game-version": 1,
                    "targets": {"rtc": {"gettimedate_fn": {"addr": "0x1FFFF4D", "size": 0xB4}}},
                    "romsize": 0x200,
                },
                generator_module=FakeGenerator,
                profile="legacy-v0.19-v0.21",
            )

    def test_invalid_generator_payload_fails_closed(self):
        class InvalidGenerator(FakeGenerator):
            PROGRAMS = [b"x" * 61, b"", b"", b""]

        with self.assertRaisesRegex(runner.GenerationError, "program table"):
            runner.serialize_patch(
                {
                    "game-code": "BPRJ", "game-version": 1,
                    "targets": {"rtc": {"gettimedate_fn": {"addr": "0x1234", "size": 0xB4}}},
                    "romsize": 0x200,
                },
                generator_module=InvalidGenerator,
                profile="legacy-v0.19-v0.21",
            )

    def test_atomic_output_refuses_overwrite_and_force_replaces(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.patch"
            runner.write_atomic(output, b"old")
            with self.assertRaisesRegex(runner.GenerationError, "already exists"):
                runner.write_atomic(output, b"new")
            self.assertEqual(output.read_bytes(), b"old")
            runner.write_atomic(output, b"new", force=True)
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["result.patch"])


if __name__ == "__main__":
    unittest.main()
