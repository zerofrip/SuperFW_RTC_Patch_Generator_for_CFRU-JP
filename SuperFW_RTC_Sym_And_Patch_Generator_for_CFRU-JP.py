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
RTC_GETTIMEDATE_MIN_SIZE = RTC_GETTIMEDATE_DAY_OFFSET + 2


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


def _rtc_day_compatibility_patch(patchset, generator_module):
    try:
        target = patchset["targets"]["rtc"]["gettimedate_fn"]
        address = target["addr"]
        size = target["size"]
        if not isinstance(target, dict) or not isinstance(address, str):
            raise ValueError("invalid target or address")
        address = int(address, 16)
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("invalid target size")
        if size < RTC_GETTIMEDATE_MIN_SIZE:
            raise ValueError("target is too small")
        if address < 0 or address + RTC_GETTIMEDATE_DAY_OFFSET + 1 > 0x1FFFFFF:
            raise ValueError("address is outside the ROM address range")
    except (KeyError, TypeError, ValueError) as error:
        raise GenerationError(
            "RTC gettimedate target is missing or malformed, or is too small for offset 0x68"
        ) from error

    try:
        return generator_module.gen_cpyhalfword(
            address + RTC_GETTIMEDATE_DAY_OFFSET,
            RTC_GETTIMEDATE_DAY_INSTRUCTION,
        )
    except Exception as error:
        raise GenerationError("Could not generate RTC day compatibility patch: %s" % error) from error


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
    rtc += _rtc_day_compatibility_patch(patchset, generator_module)

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
