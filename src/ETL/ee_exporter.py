from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from tqdm import tqdm
from typing import List, Optional, Tuple
from dataclasses import dataclass
import logging
import pandas as pd
import ee
import sys

from src.bounding_boxes import bounding_boxes
from src.ETL.ee_boundingbox import EEBoundingBox
from src.ETL import cloudfree
from src.ETL.constants import (
    START,
    END,
    LAT,
    LON,
    GEOWIKI_UNEXPORTED,
    UGANDA_UNEXPORTED,
    DEST_TIF,
)

logger = logging.getLogger(__name__)


class Season(Enum):
    in_season = "in_season"
    post_season = "post_season"


def get_user_input(text_prompt: str) -> str:
    return input(text_prompt)


@dataclass
class EarthEngineExporter:
    r"""
    Setup parameters to download cloud free sentinel data for countries,
    where countries are defined by the simplified large scale
    international boundaries.
    :param sentinel_dataset: The name of the earth engine dataset
    :param days_per_timestep: The number of days of data to use for each mosaiced image.
    :param num_timesteps: The number of timesteps to export if season is not specified
    :param fast: Whether to use the faster cloudfree exporter. This function is considerably
        faster, but cloud artefacts can be more pronounced. Default = True
    :param monitor: Whether to monitor each task until it has been run
    """
    sentinel_dataset: str
    days_per_timestep: int = 30
    num_timesteps: int = 12
    fast: bool = True
    monitor: bool = False
    credentials: Optional[str] = None
    file_dimensions: Optional[int] = None

    def check_earthengine_auth(self):
        try:
            if self.credentials:
                ee.Initialize(credentials=self.credentials)
            else:
                ee.Initialize()
        except Exception:
            logger.error(
                "This code doesn't work unless you have authenticated your earthengine account"
            )

    @staticmethod
    def cancel_all_tasks():
        ee.Initialize()
        tasks = ee.batch.Task.list()
        logger.info(f"Cancelling up to {len(tasks)} tasks")
        # Cancel running and ready tasks
        for task in tasks:
            task_id = task.status()["id"]
            task_state = task.status()["state"]
            if task_state == "RUNNING" or task_state == "READY":
                task.cancel()
                logger.info(f"Task {task_id} cancelled")
            else:
                logger.info(f"Task {task_id} state is {task_state}")

    def _export_for_polygon(
        self,
        polygon: ee.Geometry.Polygon,
        start_date: date,
        end_date: date,
        file_name_prefix: str,
        dest_bucket: Optional[str] = None,
    ):
        if self.fast:
            export_func = cloudfree.get_single_image_fast
        else:
            export_func = cloudfree.get_single_image

        image_collection_list: List[ee.Image] = []
        increment = timedelta(days=self.days_per_timestep)
        cur_date = start_date
        while (cur_date + increment) <= end_date:
            image_collection_list.append(
                export_func(region=polygon, start_date=cur_date, end_date=cur_date + increment)
            )
            cur_date += increment

        # now, we want to take our image collection and append the bands into a single image
        imcoll = ee.ImageCollection(image_collection_list)
        img = ee.Image(imcoll.iterate(cloudfree.combine_bands))

        cloudfree.export(
            image=img,
            region=polygon,
            dest_bucket=dest_bucket,
            file_name_prefix=file_name_prefix,
            monitor=self.monitor,
            file_dimensions=self.file_dimensions,
        )


@dataclass
class RegionExporter(EarthEngineExporter):
    @staticmethod
    def _start_end_dates_using_season(season: Season) -> Tuple[date, date]:
        today = date.today()
        after_april = today.month > 4
        prev_year = today.year - 1
        prev_prev_year = today.year - 2

        if season == Season.in_season:
            start_date = date(today.year if after_april else prev_year, 4, 1)
            months_between = (today.year - start_date.year) * 12 + today.month - start_date.month
            if months_between < 7:
                user_input = get_user_input(
                    f"WARNING: There are only {months_between} month(s) between today and the "
                    f"start of the season (April 1st). \nAre you sure you'd like proceed "
                    f"exporting only {months_between} months? (Y/N):\n"
                )
                if any(user_input == no for no in ["n", "N", "no", "NO"]):
                    sys.exit("Exiting script.")

            return start_date, today

        if season == Season.post_season:
            start_date = date(prev_year if after_april else prev_prev_year, 4, 1)
            end_date = date(today.year if after_april else prev_year, 4, 1)
            return start_date, end_date

        raise ValueError("Season must be in_season or post_season")

    def export(
        self,
        dest_bucket: Optional[str] = None,
        model_name: Optional[str] = None,
        end_date: Optional[date] = None,
        season: Optional[Season] = None,
        metres_per_polygon: Optional[int] = 10000,
    ):
        r"""
        Run the regional exporter. For each label, the exporter will export
        data from (end_date - timedelta(days=days_per_timestep * num_timesteps)) to end_date
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        :param dest_bucket: The name of the destination GCP bucket
        :param model_name: The name of the model that data will be fed to
        :param season: The season for which the data should be exported
        :param end_date: The end date of the data export
        :param metres_per_polygon: Whether to split the export of a large region into smaller
            boxes of (max) area metres_per_polygon * metres_per_polygon. It is better to instead
            split the area once it has been exported
        """
        if self.sentinel_dataset not in bounding_boxes:
            raise ValueError(f"{self.sentinel_dataset} was not found in bounding_boxes.py")
        region_bbox = bounding_boxes[self.sentinel_dataset]

        if season is None and end_date is None:
            raise ValueError("One of season or end_date must be specified.")

        self.check_earthengine_auth()

        if season:
            start_date, end_date = self._start_end_dates_using_season(season)
        elif end_date and self.num_timesteps:
            end_date = end_date
            start_date = end_date - timedelta(days=self.days_per_timestep * self.num_timesteps)
        else:
            raise ValueError(
                "Unable to determine start_date, either 'season' or 'end_date' and "
                "'num_timesteps' must be set."
            )

        region = EEBoundingBox.from_bounding_box(region_bbox)

        if metres_per_polygon is not None:
            regions = region.to_polygons(metres_per_patch=metres_per_polygon)
            ids = [f"{i}-{self.sentinel_dataset}" for i in range(len(regions))]
        else:
            regions = [region.to_ee_polygon()]
            ids = [self.sentinel_dataset]

        dest_folder = self.sentinel_dataset
        for identifier, region in zip(ids, regions):
            if model_name:
                dest_folder = f"{model_name}/{self.sentinel_dataset}/batch_{identifier}"

            self._export_for_polygon(
                polygon=region,
                file_name_prefix=f"{dest_folder}/{identifier}_{str(start_date)}_{str(end_date)}",
                start_date=start_date,
                end_date=end_date,
                dest_bucket=dest_bucket,
            )

        return ids


@dataclass
class LabelExporter(EarthEngineExporter):
    def export(
        self,
        labels_path: Path,
        output_folder: Path = None,
        num_labelled_points: Optional[int] = None,
        surrounding_metres: int = 80,
        start_from: Optional[int] = None,
    ):
        r"""
        Run the exporter. For each label, the exporter will export
        int( (end_date - start_date).days / days_per_timestep) timesteps of data,
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        :param labels_path: The path to the labels file
        :param output_folder: The path to the destination of the tif files
        :param num_labelled_points: (Optional) The number of labelled points to export.
        :param surrounding_metres: The number of metres surrounding each labelled point to export
        """
        self.check_earthengine_auth()

        labels = pd.read_csv(labels_path)
        if num_labelled_points:
            labels = labels[:num_labelled_points]
        if start_from:
            labels = labels[start_from:]

        if self.sentinel_dataset == "earth_engine_geowiki":
            labels = labels[~labels.index.isin(GEOWIKI_UNEXPORTED)]
        elif self.sentinel_dataset == "earth_engine_uganda":
            labels = labels[~labels.index.isin(UGANDA_UNEXPORTED)]

        # Check if exported files for labels already exist
        if output_folder and (output_folder / self.sentinel_dataset).exists():
            num_files = len(list((output_folder / self.sentinel_dataset).glob("**/*.tif")))
            if num_files >= len(labels):
                logger.info("All tif files are already exported.")
                return

        if len(labels) == 0:
            return

        already_exported = 0
        with tqdm(total=len(labels), position=0, leave=True) as pbar:
            for _, row in tqdm(labels.iterrows()):
                file_name_prefix = f"{self.sentinel_dataset}/{row[DEST_TIF]}"
                if output_folder and (output_folder / file_name_prefix).exists():
                    already_exported += 1
                    pbar.update(1)
                    continue

                bbox = EEBoundingBox.from_centre(
                    mid_lat=row[LAT], mid_lon=row[LON], surrounding_metres=surrounding_metres
                )

                start_date = datetime.strptime(row[START], "%Y-%m-%d").date()
                end_date = datetime.strptime(row[END], "%Y-%m-%d").date()
                self._export_for_polygon(
                    file_name_prefix=file_name_prefix,
                    polygon=bbox.to_ee_polygon(),
                    start_date=start_date,
                    end_date=end_date,
                )
                pbar.update(1)

        print(f"Already exported: {already_exported}")
