#!/usr/bin/env python3
"""Generate an offline SuperFW patch from a ROM and its matching .sym file."""

import argparse
import hashlib
import importlib
import os
from pathlib import Path
import struct
import sys
import tempfile

PTYPES = ("waitcnt", "irq", "swi1", "save", "layout", "rtc", "symmap")
MAGIC = b"SUPERFWPATCHV01\x00"
MAX_ROM_SIZE = 32 * 1024 * 1024
RTC_GETTIMEDATE_DAY_OFFSET = 0x68
RTC_GETTIMEDATE_DAY_INSTRUCTION = 0x1C68
RTC_GETTIMEDATE_HOOK_OFFSET = 0x3A
RTC_GETTIMEDATE_HOOK_WORD = 0xF831F000
RTC_GETTIMEDATE_TAIL_OFFSET = 0xA0
RTC_GETTIMEDATE_TAIL_WORDS = (
    0x30061C28,  # mov r0,r5; add r0,#6
    0xDF062107,  # mov r1,#7; swi 6
    0x224070E1,  # strb r1,[r4,#3]; mov r2,#0x40
    0x200071E2,  # strb r2,[r4,#7]; mov r0,#0
    0x477021B4,  # mov r1,#180; bx lr
)
RTC_GETTIMEDATE_MIN_SIZE = RTC_GETTIMEDATE_TAIL_OFFSET + len(RTC_GETTIMEDATE_TAIL_WORDS) * 4
RTC_GETTIMEDATE_HANDLER_WORDS = (
    0x4778B530, 0xE10F3000, 0xE321F09B, 0xE08EE18D, 0xE1A0500E, 0xE121F003, 0xE28F2001, 0xE12FFF12,
    0x213C1C04, 0xF827F000, 0x213C71A0, 0xF823F000, 0x21187160, 0xF81FF000, 0xF0007120, 0x31B9F831,
    0x0783A211, 0x3101D101, 0x1A6D320C, 0x186DD2F6, 0xF815F000, 0x20007020, 0x30015C11, 0xD2FB1A6D,
    0xF000186D, 0x7060F80C, 0xF0001C68, 0x70A0F808, 0xBC01BC30, 0x1C284700, 0x1C05DF06, 0x210A1C08,
    0x0100DF06, 0x47704308, 0x1E1F1C1F, 0x1F1F1E1F, 0x1F1E1F1E, 0x1E1F1D1F, 0x1F1F1E1F, 0x1F1E1F1E,
    0x30061C28, 0xDF062107, 0x224070E1, 0x200071E2, 0x477021B4,
)


class GenerationError(Exception):
    pass


def _get_data(result, ptype):
    if result is None:
        return None
    if not isinstance(result, dict) or result.get("result") != "ok":
        raise GenerationError("Analyzer %s returned malformed data" % ptype)
    data = result.get("data")
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise GenerationError("Analyzer %s returned malformed data" % ptype)
    return data


def merge_results(results):
    """Apply the browser's ptype order, including SWI1 and symmap precedence."""
    if not isinstance(results, dict) or "waitcnt" not in results:
        raise GenerationError("WAITCNT analyzer result is missing")
    patches = {}
    for ptype in PTYPES:
        if ptype not in results:
            raise GenerationError("Analyzer result is missing: %s" % ptype)
        data = _get_data(results[ptype], ptype)
        if data is not None:
            patches[ptype] = data

    waitcnt = patches.get("waitcnt")
    if waitcnt is None:
        raise GenerationError("WAITCNT analyzer returned no data")
    wait_sites = waitcnt["targets"].get("waitcnt", {}).get("patch-sites")
    if not isinstance(wait_sites, list):
        raise GenerationError("WAITCNT patch sites are missing or malformed")

    swi1 = patches.get("swi1")
    if swi1 is not None:
        swi_sites = swi1["targets"].get("waitcnt", {}).get("patch-sites")
        if not isinstance(swi_sites, list):
            raise GenerationError("SWI1 patch sites are missing or malformed")
        wait_sites.extend(swi_sites)
        del patches["swi1"]

    if patches.get("layout") is None:
        raise GenerationError("ROM layout analysis returned no data")
    symmap = patches.get("symmap")
    if symmap is None or "rtc" not in symmap["targets"]:
        raise GenerationError("Symbol map did not provide RTC targets")

    merged = {
        "game-code": waitcnt["game-code"],
        "game-version": waitcnt["game-version"],
        "files": [],
        "romsize": waitcnt["filesize"],
        "targets": {},
    }
    for ptype in PTYPES:
        if ptype in patches:
            merged["targets"].update(patches[ptype]["targets"])
    if merged["targets"].get("rtc") != symmap["targets"]["rtc"]:
        raise GenerationError("Symbol-map RTC targets did not take precedence")
    return merged


def analyze_rom(rom, sym_text, analyzers=None):
    if analyzers is None:
        analyzers = {}
        for ptype in PTYPES:
            try:
                analyzers[ptype] = importlib.import_module("patchtool." + ptype)
            except Exception as error:
                raise GenerationError("Could not load analyzer %s: %s" % (ptype, error)) from error
    results = {}
    for ptype in PTYPES:
        try:
            data = analyzers[ptype].process_rom(rom, sym=sym_text)
        except Exception as error:
            raise GenerationError("Analyzer %s failed: %s" % (ptype, error)) from error
        results[ptype] = {"result": "ok", "data": data}
    return merge_results(results)


def _rtc_relocation_patch(patchset, layout, address, size, generator_module):
    if address & 3 or size < RTC_GETTIMEDATE_MIN_SIZE or address + size > 0x2000000:
        raise GenerationError("RTC gettimedate target is unaligned or outside the ROM address range")
    if len(layout) != 1 or isinstance(layout[0], bool) or not isinstance(layout[0], int) or not 0 <= layout[0] <= 0xFFFFFFFF:
        raise GenerationError("RTC relocation requires one valid ROM layout word")

    layout_word = layout[0]
    hole_start = (layout_word >> 16) << 10
    hole_size_units = layout_word & 0xFFFF
    hole_size = hole_size_units << 10
    hole_end = hole_start + hole_size
    if hole_size_units <= 1 or hole_start & 0x3FF or hole_size & 0x3FF or hole_end > 0x2000000:
        raise GenerationError("RTC relocation layout hole is malformed or too small")

    relocated = hole_end - 0x400
    handler_end = relocated + len(RTC_GETTIMEDATE_HANDLER_WORDS) * 4
    if relocated >= 0x1000000 or handler_end > 0x1000000:
        raise GenerationError("RTC relocation address must remain below 16 MiB")

    rtc_targets = patchset.get("targets", {}).get("rtc", {})
    for name, function in rtc_targets.items():
        if not isinstance(function, dict) or not isinstance(function.get("addr"), str):
            raise GenerationError("RTC handler target is malformed")
        try:
            function_address = int(function["addr"], 16)
            function_size = function["size"]
        except (KeyError, TypeError, ValueError) as error:
            raise GenerationError("RTC handler target is malformed") from error
        if isinstance(function_size, bool) or not isinstance(function_size, int) or function_size <= 0:
            raise GenerationError("RTC handler target is malformed")
        if relocated < function_address + function_size and function_address < handler_end:
            raise GenerationError("RTC relocation overlaps an existing RTC handler")

    layout[0] = (layout_word & 0xFFFF0000) | (hole_size_units - 1)
    raw_patch = []
    for index in range(0, len(RTC_GETTIMEDATE_HANDLER_WORDS), 8):
        words = list(RTC_GETTIMEDATE_HANDLER_WORDS[index:index + 8])
        raw_patch += generator_module.gen_cpywords(relocated + index * 4, words)
    raw_patch += generator_module.gen_cpywords(
        address, [0x47184B00, 0x08000000 + relocated | 1]
    )
    return raw_patch


def _rtc_compatibility_patch(patchset, generator_module, layout):
    try:
        target = patchset["targets"]["rtc"]["gettimedate_fn"]
        if not isinstance(target, dict):
            raise ValueError("invalid target")
        address = target["addr"]
        size = target["size"]
        if not isinstance(address, str):
            raise ValueError("invalid target or address")
        address = int(address, 16)
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("invalid target size")
        if size < RTC_GETTIMEDATE_MIN_SIZE:
            raise ValueError("target is too small")
        if address < 0 or address + RTC_GETTIMEDATE_MIN_SIZE - 1 > 0x1FFFFFF:
            raise ValueError("address is outside the ROM address range")
        high_rom = address >= 0x1000000
    except (KeyError, TypeError, ValueError) as error:
        raise GenerationError(
            "RTC gettimedate target is missing or malformed, or is too small through offset 0xB3"
        ) from error

    if high_rom:
        try:
            return _rtc_relocation_patch(patchset, layout, address, size, generator_module)
        except GenerationError:
            raise
        except Exception as error:
            raise GenerationError("Could not generate RTC relocation patch: %s" % error) from error

    try:
        # These raw words are intentional: gen_cpywords writes little-endian Thumb
        # instruction bytes, including the fixed BL encoding used by these builds.
        return (
            generator_module.gen_cpyhalfword(
                address + RTC_GETTIMEDATE_DAY_OFFSET,
                RTC_GETTIMEDATE_DAY_INSTRUCTION,
            )
            + generator_module.gen_cpywords(
                address + RTC_GETTIMEDATE_HOOK_OFFSET,
                [RTC_GETTIMEDATE_HOOK_WORD],
            )
            + generator_module.gen_cpywords(
                address + RTC_GETTIMEDATE_TAIL_OFFSET,
                list(RTC_GETTIMEDATE_TAIL_WORDS),
            )
        )
    except Exception as error:
        raise GenerationError("Could not generate RTC compatibility patch: %s" % error) from error


def serialize_patch(patchset, generator_module=None):
    if generator_module is None:
        try:
            generator_module = importlib.import_module("patchtool.generator")
        except Exception as error:
            raise GenerationError("Could not load official patch generator: %s" % error) from error
    try:
        game_patch = generator_module.GamePatch(
            patchset["game-code"], patchset["game-version"],
            patchset["targets"], patchset["romsize"])
        waitcnt = game_patch.waitcnt_patches()
        save = game_patch.save_patches()
        irq = game_patch.irq_patches()
        rtc = list(game_patch.rtc_patches())
        layout = game_patch.layout_patches()
        save_type = game_patch.save_type
        programs = generator_module.PROGRAMS[:4]
    except Exception as error:
        raise GenerationError("GamePatch failed: %s" % error) from error
    rtc += _rtc_compatibility_patch(patchset, generator_module, layout)

    counts = (len(waitcnt), len(save), save_type, len(irq), len(rtc))
    if any(not isinstance(value, int) or value < 0 or value > 255 for value in counts):
        raise GenerationError("Patch header count is outside the byte range")
    if len(layout) > 1 or (layout and (not isinstance(layout[0], int) or not 0 <= layout[0] <= 0xFFFFFFFF)):
        raise GenerationError("ROM layout header value is invalid")
    if len(programs) != 4 or any(len(program) > 60 for program in programs):
        raise GenerationError("Official program table is invalid")

    try:
        header = MAGIC + struct.pack(
            "<BBBBBxIxxxxxx", len(waitcnt), len(save), save_type,
            len(irq), len(rtc), layout[0] if layout else 0)
        for program in programs:
            header += struct.pack("<I", len(program)) + program + bytes(60 - len(program))
        words = waitcnt + save + irq + rtc
        content = b"".join(struct.pack("<I", word) for word in words)
    except (struct.error, TypeError) as error:
        raise GenerationError("Patch payload contains invalid data: %s" % error) from error
    if len(content) > 512:
        raise GenerationError("Patch payload exceeds 512 bytes")
    payload = header + content + bytes(512 - len(content))
    if len(payload) != 800:
        raise GenerationError("Serialized patch has an invalid size")
    return payload, {
        "waitcnt": len(waitcnt), "save": len(save), "save_type": save_type,
        "irq": len(irq), "rtc": len(rtc), "layout": len(layout),
    }


def build_patch(rom, sym_text, analyzers=None, generator_module=None):
    patchset = analyze_rom(rom, sym_text, analyzers=analyzers)
    return serialize_patch(patchset, generator_module=generator_module)


def write_atomic(path, data, force=False):
    path = Path(path)
    if not path.parent.is_dir():
        raise GenerationError("Output directory does not exist: %s" % path.parent)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if force:
            os.replace(temp_name, str(path))
        elif os.name == "nt":
            # Windows rename is atomic and fails rather than replacing an existing file.
            os.rename(temp_name, str(path))
        else:
            # POSIX rename replaces files, so use a hard link to refuse a race.
            os.link(temp_name, str(path))
            os.unlink(temp_name)
    except FileExistsError as error:
        raise GenerationError("Output already exists; use --force to replace it: %s" % path) from error
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_inputs(rom_path, sym_path):
    rom_path = Path(rom_path)
    if rom_path.suffix.lower() != ".gba":
        raise GenerationError("Input must have a .gba extension")
    try:
        with rom_path.open("rb") as source:
            rom = source.read(MAX_ROM_SIZE + 1)
        if len(rom) < 0xC0 or len(rom) > MAX_ROM_SIZE:
            raise GenerationError("Input ROM must be between 0xC0 bytes and 32 MiB")
        with Path(sym_path).open("r", encoding="utf-8", errors="strict", newline="") as source:
            sym_text = source.read()
    except (OSError, UnicodeError) as error:
        raise GenerationError("Could not read input: %s" % error) from error
    return rom, sym_text


def main(argv=None):
    if sys.version_info < (3, 8):
        print("ERROR: Python 3.8 or newer is required.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", required=True, help="input .gba (read-only)")
    parser.add_argument("--sym", required=True, help="matching generated .sym file")
    parser.add_argument("--output", required=True, help="output .patch file")
    parser.add_argument("--force", action="store_true", help="replace an existing output patch")
    args = parser.parse_args(argv)

    try:
        rom, sym_text = read_inputs(args.rom, args.sym)
        patch, counts = build_patch(rom, sym_text)
        write_atomic(args.output, patch, force=args.force)
    except Exception as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1

    print("ROM SHA-256: %s" % hashlib.sha256(rom).hexdigest())
    print("SYM SHA-256: %s" % hashlib.sha256(sym_text.encode("utf-8")).hexdigest())
    print("PATCH SHA-256: %s" % hashlib.sha256(patch).hexdigest())
    print("Counts: waitcnt={waitcnt} save={save} save_type={save_type} irq={irq} rtc={rtc} layout={layout}".format(**counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
