"""Поддельные /proc/meminfo и /proc/cpuinfo: «мощность компьютера» для тестов.

Программа читает их через переменную SUPERAPP_PROC_ROOT (drvsearch/hwcheck.py).
"""

import os

GIB = 1 << 30
FLAGS = "fpu vme de pse tsc msr pae mce cx8 sse sse2 ssse3 sse4_1 sse4_2 avx f16c fma avx2"


def make_proc(root, total_gib=16, avail_gib=12, flags=FLAGS):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "meminfo"), "w") as f:
        f.write("MemTotal:       %d kB\nMemFree:        %d kB\nMemAvailable:   %d kB\n"
                % (total_gib * GIB // 1024, avail_gib * GIB // 1024, avail_gib * GIB // 1024))
    with open(os.path.join(root, "cpuinfo"), "w") as f:
        f.write("processor\t: 0\nmodel name\t: Fake CPU @ 3.0GHz\nflags\t\t: %s\n\n" % flags)
    return root
