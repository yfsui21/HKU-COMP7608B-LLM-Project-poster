#!/bin/sh
# Demo input: the PosterForest paper itself (23 pages, 6 figures, 2 tables).
# Too large (49MB) to keep in git.
set -e
mkdir -p samples
curl -L -o samples/posterforest.pdf https://arxiv.org/pdf/2508.21720v3
echo "wrote samples/posterforest.pdf"
