"""Native image libraries shared by thumbnail, cover and download workers."""
import os

# OpenBLAS allocates a workspace for each native thread at import time.
# The service already runs up to two image jobs concurrently.
if not any(os.environ.get(name) for name in
           ('OPENBLAS_NUM_THREADS', 'OPENBLAS_DEFAULT_NUM_THREADS', 'GOTO_NUM_THREADS', 'OMP_NUM_THREADS')):
    os.environ['OPENBLAS_NUM_THREADS'] = '1'

import cv2
import numpy as np

if not os.environ.get('OPENCV_FOR_THREADS_NUM') and cv2.getNumThreads() > 2:
    cv2.setNumThreads(2)
