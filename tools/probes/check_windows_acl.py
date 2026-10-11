"""Read-only access-handle diagnostic; never repairs or changes ACLs."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


def inspect_access(path: Path) -> dict:
    if os.name != 'nt':
        raise RuntimeError('This diagnostic requires Windows')
    if not path.is_dir():
        raise ValueError('Choose an existing workspace directory')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    result = {}
    for name, mask in [('READ_CONTROL', 0x20000), ('WRITE_DAC', 0x40000), ('WRITE_OWNER', 0x80000)]:
        handle = kernel.CreateFileW(str(path.resolve()), mask, 7, None, 3, 0x2000000, None)
        error = ctypes.get_last_error()
        allowed = handle != ctypes.c_void_p(-1).value
        if allowed:
            kernel.CloseHandle(handle)
        result[name] = {'allowed': allowed, 'win32_error': 0 if allowed else error}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_access(args.workspace)))
