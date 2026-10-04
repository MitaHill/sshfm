"""Name ordering from MitaHill/sshfm's generated colltab.inc.

Source: fd263ad0a85d36cce78921973f1612b353e12831. Do not hand-edit the table.
"""
from importlib.resources import files
import zlib

_data = zlib.decompress(files(__package__).joinpath('collation.bin.zlib').read_bytes())
_ranks = {int.from_bytes(_data[i:i + 3], 'little'): i // 3
          for i in range(0, len(_data), 3)}
del _data


def name_key(name):
    return tuple(_ranks.get(ord(c), 0xFFFFFFFF) for c in name), name.encode('utf-8')
