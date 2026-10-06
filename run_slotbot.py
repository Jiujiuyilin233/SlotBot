"""Source and frozen Windows entrypoint."""
import os

# Cap BLAS / OpenMP worker pools before NumPy imports them. These libraries size
# their thread pools to the CPU count by default; on many-core machines that
# wastes tens of MB of per-thread scratch and causes oversubscription during the
# frequent small image ops. A small fixed cap lowers the steady-state footprint.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "2")

from slotbot import main

if __name__ == "__main__":
    main()
