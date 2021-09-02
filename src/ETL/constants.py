# df column names
SOURCE = "source"
CROP_PROB = "crop_probability"
START = "start_date"
END = "end_date"
LON = "lon"
LAT = "lat"
COUNTRY = "country"
NUM_LABELERS = "num_labelers"
SUBSET = "subset"
DATASET = "dataset"
DEST_FOLDER = "dest_tif"
DEST_TIF = "dest_tif"

BANDS = [
    "B1",
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "B7",
    "B8",
    "B8A",
    "B9",
    "B10",
    "B11",
    "B12",
]

# 9 images are not exported in geowiki due to:
# Error: Image.select: Pattern 'B1' did not match any bands.
GEOWIKI_UNEXPORTED = [35684, 35687, 35705, 35717, 35726, 35730, 35791, 35861, 35865]
UGANDA_UNEXPORTED = [2856, 2879, 2943, 2944, 2945, 2951, 2987]
