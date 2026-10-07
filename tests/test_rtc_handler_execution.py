"""Execute the emitted ARM/Thumb RTC handler when Unicorn is available."""

import datetime as dt
import importlib.util
from pathlib import Path
import struct
import unittest

try:
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_INTR
    from unicorn.arm_const import (
        UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,
        UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
    )
except ImportError:
    Uc = None


GENERATOR = Path(__file__).resolve().parents[1] / "SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.py"
SPEC = importlib.util.spec_from_file_location("rtc_generator", GENERATOR)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

ROM = 0x08000000
RAM = 0x02000000
RETURN = RAM + 0x1F00
CODE = struct.pack("<47I", *runner.RTC_GETTIMEDATE_0212_HANDLER_WORDS)


def _new_cpu():
    cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    cpu.mem_map(ROM, 0x1000)
    cpu.mem_map(RAM, 0x2000)
    cpu.mem_write(ROM, CODE)

    def arm7_bx_pc(cpu, address, size, _):
        # ARM7TDMI ignores bit 1 of BX PC's target when entering ARM state.
        if address == ROM + 2:
            cpu.reg_write(UC_ARM_REG_CPSR, cpu.reg_read(UC_ARM_REG_CPSR) & ~0x20)
            cpu.reg_write(UC_ARM_REG_PC, ROM + 4)
        elif address == ROM + 36:
            # ARM7 reads PC two bytes ahead here after BX PC at a halfword boundary.
            # Unicorn does not model that pipeline skew, so execute this one
            # interworking calculation with the real hardware PC value.
            opcode = struct.unpack_from("<I", CODE, 36)[0]
            assert opcode == 0xE24F2001
            cpu.reg_write(UC_ARM_REG_R2, ROM + 46 - 1)
            cpu.reg_write(UC_ARM_REG_PC, ROM + 40)

    cpu.hook_add(UC_HOOK_CODE, arm7_bx_pc)
    return cpu


def _swi_division(cpu, _interrupt, _):
    pc = cpu.reg_read(UC_ARM_REG_PC)
    assert bytes(cpu.mem_read(pc - 2, 2)) == b"\x06\xdf"
    numerator, denominator = (cpu.reg_read(register) for register in (UC_ARM_REG_R0, UC_ARM_REG_R1))
    numerator = numerator if numerator < 0x80000000 else numerator - 0x100000000
    denominator = denominator if denominator < 0x80000000 else denominator - 0x100000000
    quotient = abs(numerator) // abs(denominator) * (-1 if (numerator < 0) != (denominator < 0) else 1)
    cpu.reg_write(UC_ARM_REG_R0, quotient & 0xFFFFFFFF)
    cpu.reg_write(UC_ARM_REG_R1, (numerator - quotient * denominator) & 0xFFFFFFFF)
    cpu.reg_write(UC_ARM_REG_R3, abs(quotient))


def _bcd(value):
    return value // 10 * 16 + value % 10


@unittest.skipUnless(Uc is not None, "optional dependency: unicorn")
class HandlerExecutionTests(unittest.TestCase):
    def test_calendar_and_status(self):
        epoch = dt.datetime(2000, 1, 1)
        dates = [
            dt.datetime(2000, 1, 1), dt.datetime(2000, 2, 28),
            dt.datetime(2000, 2, 29), dt.datetime(2000, 3, 1),
            dt.datetime(2000, 3, 31), dt.datetime(2000, 4, 1),
            dt.datetime(2028, 2, 28), dt.datetime(2028, 2, 29),
            dt.datetime(2028, 3, 1), dt.datetime(2028, 3, 31),
            dt.datetime(2028, 4, 1), dt.datetime(2026, 10, 6, 22, 22),
            dt.datetime(2030, 2, 3, 15, 34), dt.datetime(2030, 3, 31),
            dt.datetime(2030, 4, 1), dt.datetime(2030, 12, 31, 23, 59, 59),
            dt.datetime(2031, 1, 1),
        ]
        for date in dates:
            timestamp = int((date - epoch).total_seconds())
            expected = bytes((
                _bcd(date.year - 2000), _bcd(date.month), _bcd(date.day),
                (date.weekday() + 1) % 7, _bcd(date.hour), _bcd(date.minute),
                _bcd(date.second), 0x40,
            ))
            for speed in range(6):
                for carry in (0, 1):
                    with self.subTest(date=date, speed=speed, carry=carry):
                        cpu = _new_cpu()
                        cpu.hook_add(UC_HOOK_INTR, _swi_division)
                        cpu.reg_write(UC_ARM_REG_CPSR, 0x9B)
                        cpu.reg_write(UC_ARM_REG_LR, timestamp)
                        cpu.reg_write(UC_ARM_REG_SP, 0x580 | speed)
                        cpu.reg_write(UC_ARM_REG_CPSR, 0x1F | carry << 29)
                        cpu.reg_write(UC_ARM_REG_SP, RAM + 0x1800)
                        cpu.reg_write(UC_ARM_REG_LR, RETURN | 1)
                        cpu.reg_write(UC_ARM_REG_R0, RAM)
                        cpu.emu_start(ROM | 1, RETURN, count=100000)
                        self.assertEqual(cpu.reg_read(UC_ARM_REG_PC), RETURN)
                        self.assertEqual(bytes(cpu.mem_read(RAM, 8)), expected)

    def test_timestamp_advances_independently_of_incoming_carry(self):
        for speed in range(6):
            for carry in (0, 1):
                with self.subTest(speed=speed, carry=carry):
                    cpu = _new_cpu()
                    cpu.reg_write(UC_ARM_REG_CPSR, 0x9B)
                    cpu.reg_write(UC_ARM_REG_SP, 0x580 | speed)
                    cpu.reg_write(UC_ARM_REG_LR, 1000)
                    expected_timestamp, budget = 1000, 176
                    for _ in range(180):
                        cpu.reg_write(UC_ARM_REG_CPSR, 0x1F | carry << 29)
                        cpu.reg_write(UC_ARM_REG_SP, RAM + 0x1800)
                        cpu.reg_write(UC_ARM_REG_LR, RETURN | 1)
                        cpu.reg_write(UC_ARM_REG_R0, RAM)
                        cpu.emu_start(ROM | 1, ROM + 44, count=100)
                        self.assertEqual(cpu.reg_read(UC_ARM_REG_PC), ROM + 44)
                        budget -= speed
                        if budget < 0:
                            expected_timestamp += 1
                            budget += 176
                        cpu.reg_write(UC_ARM_REG_CPSR, 0x9B)
                        self.assertEqual(cpu.reg_read(UC_ARM_REG_LR), expected_timestamp)
                        self.assertEqual(cpu.reg_read(UC_ARM_REG_SP), budget * 8 | speed)
