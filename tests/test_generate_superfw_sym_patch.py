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

        patch, counts = runner.build_patch(bytes(rom), symbols)

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
            self.assertIn("waitcnt=0 save=0 save_type=0 irq=0 rtc=14 layout=0", stdout.getvalue())

            patch_path.unlink()
            sym_path.write_text("", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = runner.main(["--rom", str(rom_path), "--sym", str(sym_path), "--output", str(patch_path)])
            self.assertEqual(status, 1)
            self.assertFalse(patch_path.exists())
            self.assertIn("did not provide RTC", stderr.getvalue())


class SerializationTests(unittest.TestCase):
    def test_serialization_matches_800_byte_header_programs_and_payload(self):
        patch, counts = runner.serialize_patch(
            {
                "game-code": "BPRJ", "game-version": 1,
                "targets": {"rtc": {"gettimedate_fn": {"addr": "0x1234", "size": 0xB4}}},
                "romsize": 0x200,
            },
            generator_module=FakeGenerator,
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
