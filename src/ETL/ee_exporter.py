from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from tqdm import tqdm
from typing import List, Optional, Tuple, Union
import logging
import pandas as pd
import ee
import xarray as xr
import geopandas
import sys

from src.ETL.ee_boundingbox import BoundingBox, EEBoundingBox
from src.ETL import cloudfree

logger = logging.getLogger(__name__)


class Season(Enum):
    in_season = "in_season"
    post_season = "post_season"


def get_user_input(text_prompt: str) -> str:
    return input(text_prompt)


class EarthEngineExporter:

    min_date = date(2017, 3, 28)

    def __init__(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        end_month_day: Optional[Tuple[int, int]] = None,
        season: Optional[Season] = None,
        days_per_timestep: int = 30,
        num_timesteps: int = 12,
        additional_cols: List[str] = [],
        region_bbox: Optional[BoundingBox] = None,
    ):
        r"""
        Setup parameters to download cloud free sentinel data for countries,
        where countries are defined by the simplified large scale
        international boundaries.

        :param start_date: The start date of the data export
        :param end_date: The end date of the data export
        :param end_date: The end date without specifying the year of the data export
        :param season: The season to use to determine the start and end date
        :param days_per_timestep: The number of days of data to use for each mosaiced image.
        :param num_timesteps: The number of timesteps to export if season is not specified
        :param additional_cols: The additional columns to extract when creating the dataframe
        :param region_bbox: BoundingBox for region
        """

        if end_date is None and end_month_day is None and season is None:
            raise ValueError("One of end_date, end_month_day, season must not be None")

        self.additional_cols = additional_cols
        self.days_per_timestep = days_per_timestep
        self.num_timesteps = num_timesteps
        self.start_date = start_date
        self.end_date = end_date
        self.end_month_day = end_month_day
        self.season = season
        self.region_bbox = region_bbox

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

    def _load_labels(self, labels_path) -> pd.DataFrame:
        if not labels_path.exists():
            raise FileExistsError(f"Could not find labels file: {labels_path}")
        elif labels_path.suffix == ".nc":
            return xr.open_dataset(labels_path).to_dataframe().dropna().reset_index()
        elif labels_path.suffix == ".geojson":
            return geopandas.read_file(labels_path)[["lat", "lon"] + self.additional_cols]
        else:
            raise ValueError(f"Unexpected extension {labels_path.suffix}")

    @staticmethod
    def _date_overlap(start1: date, end1: date, start2: date, end2: date) -> int:
        overlaps = start1 <= end2 and end1 >= start2
        if not overlaps:
            return 0
        overlap_days = (min(end1, end2) - max(start1, start2)).days
        return overlap_days

    @staticmethod
    def _end_date_using_max_overlap(
        planting_date: date,
        harvest_date: date,
        end_month_day: Tuple[int, int],
        total_days: timedelta,
    ):
        potential_end_dates = [
            date(harvest_date.year + diff, *end_month_day) for diff in range(-1, 2)
        ]
        potential_end_dates = [d for d in potential_end_dates if d < datetime.now().date()]
        end_date = max(
            potential_end_dates,
            key=lambda d: EarthEngineExporter._date_overlap(
                planting_date, harvest_date, d - total_days, d
            ),
        )
        return end_date

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

    def _labels_to_bounding_boxes(
        self, labels, num_labelled_points: Optional[int], surrounding_metres: int
    ) -> List[Tuple[int, EEBoundingBox, date, date]]:
        output: List[Tuple[int, EEBoundingBox, date, date]] = []

        start_date, end_date = self.start_date, self.end_date
        total_days = timedelta(days=self.num_timesteps * self.days_per_timestep)
        if start_date is None and end_date:
            start_date = end_date - total_days

        if start_date:
            assert (
                start_date >= self.min_date
            ), f"Sentinel data does not exist before {self.min_date}"

        for idx, row in tqdm(labels.iterrows()):
            if self.end_month_day:
                if "harvest_da" not in row or "planting_d" not in row:
                    continue
                planting_date = datetime.strptime(row["planting_d"], "%Y-%m-%d %H:%M:%S").date()
                harvest_date = datetime.strptime(row["harvest_da"], "%Y-%m-%d %H:%M:%S").date()
                end_date = self._end_date_using_max_overlap(
                    planting_date, harvest_date, self.end_month_day, total_days
                )
                start_date = end_date - total_days if end_date else None

            if start_date and end_date:
                output.append(
                    (
                        idx,
                        EEBoundingBox.from_centre(
                            mid_lat=row["lat"],
                            mid_lon=row["lon"],
                            surrounding_metres=surrounding_metres,
                        ),
                        start_date,
                        end_date,
                    )
                )
            if num_labelled_points is not None:
                if len(output) >= num_labelled_points:
                    return output
        return output

    def export_for_labels(
        self,
        labels_path: Path,
        output_folder: Path,
        sentinel_dataset: str,
        num_labelled_points: Optional[int] = None,
        surrounding_metres: int = 80,
        checkpoint: bool = True,
        monitor: bool = False,
        fast: bool = True,
    ):
        r"""
        Run the exporter. For each label, the exporter will export
        int( (end_date - start_date).days / days_per_timestep) timesteps of data,
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        :param labels_path: The path to the labels file
        :param output_folder: The folder to export the earth engine data to
        :param sentinel_dataset: The name of the earth engine dataset
        :param num_labelled_points: (Optional) The number of labelled points to export.
        :param surrounding_metres: The number of metres surrounding each labelled point to export
        :param checkpoint: Whether or not to check in self.data_folder to see if the file has
            already been exported. If it has, skip it
        :param monitor: Whether to monitor each task until it has been run
        :param fast: Whether to use the faster cloudfree exporter. This function is considerably
            faster, but cloud artefacts can be more pronounced. Default = True
        """
        try:
            ee.Initialize()
        except Exception:
            logger.error(
                "This code doesn't work unless you have authenticated your earthengine account"
            )

        labels = self._load_labels(labels_path)

        bounding_boxes_to_download = self._labels_to_bounding_boxes(
            labels=labels,
            num_labelled_points=num_labelled_points,
            surrounding_metres=surrounding_metres,
        )

        for idx, bbox, start_date, end_date in bounding_boxes_to_download:
            self._export_for_polygon(
                output_folder=output_folder,
                sentinel_dataset=sentinel_dataset,
                polygon=bbox.to_ee_polygon(),
                polygon_identifier=idx,
                start_date=start_date,
                end_date=end_date,
                checkpoint=checkpoint,
                monitor=monitor,
                fast=fast,
            )

    def export_for_region(
        self,
        sentinel_dataset: str,
        output_folder: Path,
        checkpoint: bool = True,
        monitor: bool = True,
        metres_per_polygon: Optional[int] = 10000,
        fast: bool = True,
    ):
        r"""
        Run the regional exporter. For each label, the exporter will export
        data from (end_date - timedelta(days=days_per_timestep * num_timesteps)) to end_date
        where each timestep consists of a mosaic of all available images within the
        days_per_timestep of that timestep.
        :param sentinel_dataset: The name of the region to export.
        :param checkpoint: Whether or not to check in self.data_folder to see if the file has
            already been exported. If it has, skip it
        :param monitor: Whether to monitor each task until it has been run
        :param metres_per_polygon: Whether to split the export of a large region into smaller
            boxes of (max) area metres_per_polygon * metres_per_polygon. It is better to instead
            split the area once it has been exported
        :param fast: Whether to use the faster cloudfree exporter. This function is considerably
            faster, but cloud artefacts can be more pronounced. Default = True
        """
        try:
            ee.Initialize()
        except Exception:
            logger.error(
                "This code doesn't work unless you have authenticated your earthengine account"
            )

        if self.season:
            start_date, end_date = self._start_end_dates_using_season(self.season)
        elif self.end_date and self.num_timesteps:
            end_date = self.end_date
            start_date = self.end_date - self.num_timesteps * timedelta(days=self.days_per_timestep)
        else:
            raise ValueError(
                "Unable to determine start_date, either 'season' or 'end_date' and "
                "'num_timesteps' must be set."
            )

        if self.region_bbox is None:
            raise ValueError("Region bbox must be set to export_for_region")

        region = EEBoundingBox.from_bounding_box(self.region_bbox)

        if metres_per_polygon is not None:

            regions = region.to_polygons(metres_per_patch=metres_per_polygon)

            for idx, region in enumerate(regions):
                self._export_for_polygon(
                    output_folder=output_folder,
                    sentinel_dataset=sentinel_dataset,
                    polygon=region,
                    polygon_identifier=f"{idx}-{sentinel_dataset}",
                    start_date=start_date,
                    end_date=end_date,
                    checkpoint=checkpoint,
                    monitor=monitor,
                    fast=fast,
                )
        else:
            self._export_for_polygon(
                output_folder=output_folder,
                sentinel_dataset=sentinel_dataset,
                polygon=region.to_ee_polygon(),
                polygon_identifier=sentinel_dataset,
                start_date=start_date,
                end_date=end_date,
                checkpoint=checkpoint,
                monitor=monitor,
                fast=fast,
            )

    def _export_for_polygon(
        self,
        output_folder: Path,
        sentinel_dataset: str,
        polygon: ee.Geometry.Polygon,
        polygon_identifier: Union[int, str],
        start_date: date,
        end_date: date,
        checkpoint: bool,
        monitor: bool,
        fast: bool,
    ):

        if fast:
            export_func = cloudfree.get_single_image_fast
        else:
            export_func = cloudfree.get_single_image

        cur_date = start_date
        cur_end_date = cur_date + timedelta(days=self.days_per_timestep)

        image_collection_list: List[ee.Image] = []

        logger.info(
            f"Exporting image for polygon {polygon_identifier} from "
            f"aggregated images between {str(cur_date)} and {str(end_date)}"
        )
        filename = f"{polygon_identifier}_{str(cur_date)}_{str(end_date)}"

        if checkpoint and (output_folder / f"{filename}.tif").exists():
            logger.warning("File already exists! Skipping")
            return None

        while cur_end_date <= end_date:

            image_collection_list.append(
                export_func(region=polygon, start_date=cur_date, end_date=cur_end_date)
            )
            cur_date += timedelta(days=self.days_per_timestep)
            cur_end_date += timedelta(days=self.days_per_timestep)

        # now, we want to take our image collection and append the bands into a single image
        imcoll = ee.ImageCollection(image_collection_list)
        img = ee.Image(imcoll.iterate(cloudfree.combine_bands))

        # and finally, export the image
        cloudfree.export(
            image=img,
            region=polygon,
            filename=filename,
            drive_folder=sentinel_dataset,
            monitor=monitor,
        )