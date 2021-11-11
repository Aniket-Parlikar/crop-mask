"""
Combines the earth observation data with the labels to create (x, y) training data
"""
import os
import sys

# Change the working directory to the directory of this script
os.chdir(os.path.dirname(os.path.realpath(__file__)))

sys.path.append("..")

from src.datasets_labeled import labeled_datasets  # noqa: E402

if __name__ == "__main__":
    for d in labeled_datasets:
        d.create_features()