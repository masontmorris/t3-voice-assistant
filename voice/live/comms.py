"""The names of Windows' default communications devices, the microphone and speakers chosen for calls.

PortAudio only reports the console defaults, so this asks the Windows audio device enumerator directly. Only the
device choice comes from here: the stream itself stays raw, since a communications-category stream is exactly what
Windows applies its call processing to.
"""

import ctypes
import sys
import uuid
from ctypes import POINTER, byref, c_uint, c_void_p, c_wchar_p

CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMM_DEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
FRIENDLY_NAME_FMTID = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
FRIENDLY_NAME_PID = 14
RENDER, CAPTURE = 0, 1
COMMUNICATIONS = 2
CLSCTX_ALL = 23
VT_LPWSTR = 31


class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text):
        return cls((ctypes.c_ubyte * 16)(*uuid.UUID(text).bytes_le))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


def _method(obj, index, *argtypes):
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])


def _release(obj):
    if obj:
        ctypes.WINFUNCTYPE(c_uint, c_void_p)(ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents[2])(obj)


def default_name(flow):
    """The friendly name of the default communications endpoint for `flow` (CAPTURE or RENDER), or None."""
    if sys.platform != "win32":
        return None
    ole32 = ctypes.OleDLL("ole32")
    try:
        ole32.CoInitializeEx(None, 0)
    except OSError:
        pass
    enumerator, device, store = c_void_p(), c_void_p(), c_void_p()
    try:
        ole32.CoCreateInstance(byref(GUID.of(CLSID_MM_DEVICE_ENUMERATOR)), None, CLSCTX_ALL,
                               byref(GUID.of(IID_IMM_DEVICE_ENUMERATOR)), byref(enumerator))
        _method(enumerator, 4, c_uint, c_uint, POINTER(c_void_p))(enumerator, flow, COMMUNICATIONS, byref(device))
        _method(device, 4, c_uint, POINTER(c_void_p))(device, 0, byref(store))
        key = PROPERTYKEY(GUID.of(FRIENDLY_NAME_FMTID), FRIENDLY_NAME_PID)
        value = (ctypes.c_ubyte * 32)()
        _method(store, 5, POINTER(PROPERTYKEY), c_void_p)(store, byref(key), value)
        if int.from_bytes(bytes(value[:2]), "little") != VT_LPWSTR:
            return None
        name = c_wchar_p(int.from_bytes(bytes(value[8:16]), "little")).value
        ole32.PropVariantClear(value)
        return name
    except OSError:
        return None
    finally:
        for obj in (store, device, enumerator):
            _release(obj)
