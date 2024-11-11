import logging
from pathlib import Path

import polars as pl
import reactable
from matplotlib.backends.backend_pdf import PdfPages
from patito.exceptions import DataFrameValidationError

from ski_bearings.bearing import (
    get_bearing_distributions_df,
    get_bearing_summary_stats,
)
from ski_bearings.models import BearingStatsModel, SkiAreaModel
from ski_bearings.openskimap_utils import (
    get_ski_area_to_runs,
    load_downhill_ski_areas_from_download_pl,
    load_runs_from_download_pl,
)
from ski_bearings.osmnx_utils import (
    create_networkx_with_metadata,
)
from ski_bearings.plot import subplot_orientations
from ski_bearings.utils import get_data_directory


def get_ski_area_metrics_path(testing: bool = False) -> Path:
    return get_data_directory(testing=testing).joinpath("ski_area_metrics.parquet")


def get_runs_parquet_path(testing: bool = False) -> Path:
    return get_data_directory(testing=testing).joinpath("runs.parquet")


def analyze_all_ski_areas(skip_runs: bool = False) -> None:
    """
    Analyze ski areas to create a table of ski areas and their metrics
    including bearing distributions.
    Keyed on ski_area_id.
    Write data as parquet.
    """
    if not skip_runs:
        runs_df = load_runs_from_download_pl()
        runs_path = get_runs_parquet_path()
        logging.info(f"Writing {runs_path}")
        runs_df.write_parquet(runs_path)
        del runs_df

    ski_area_df = load_downhill_ski_areas_from_download_pl()
    ski_area_metadatas = {x["ski_area_id"]: x for x in ski_area_df.to_dicts()}
    ski_area_to_runs = get_ski_area_to_runs(runs_pl=load_runs_pl())
    bearing_dist_dfs = []
    ski_area_metrics = []
    for ski_area_id, ski_area_metadata in ski_area_metadatas.items():
        logging.info(
            f"Analyzing {ski_area_id} named {ski_area_metadata['ski_area_name']}"
        )
        ski_area_runs = ski_area_to_runs.get(ski_area_id, [])
        ski_area_metadata = ski_area_metadatas[ski_area_id]
        graph = create_networkx_with_metadata(
            runs=ski_area_runs, ski_area_metadata=ski_area_metadata
        )
        ski_area_metrics.append(graph.graph)
        bearing_dist_df = get_bearing_distributions_df(graph).select(
            pl.lit(ski_area_id).alias("ski_area_id"),
            pl.all(),
        )
        bearing_dist_dfs.append(bearing_dist_df)
    logging.info("Creating ski area metrics dataframe with bearing distributions.")
    bearing_dist_df = (
        pl.concat(bearing_dist_dfs, how="vertical_relaxed")
        .group_by("ski_area_id")
        .agg(pl.struct(pl.exclude("ski_area_id")).alias("bearings"))
    )
    ski_area_metrics_df = (
        pl.DataFrame(data=ski_area_metrics)
        .with_columns(websites=pl.col("websites").list.drop_nulls())
        .join(bearing_dist_df, on="ski_area_id", how="left")
    )
    try:
        SkiAreaModel.validate(ski_area_metrics_df, allow_superfluous_columns=True)
    except DataFrameValidationError as exc:
        logging.error(f"SkiAreaModel.validate failed with {exc}")
    ski_area_metrics_path = get_ski_area_metrics_path()
    logging.info(f"Writing {ski_area_metrics_path}")
    ski_area_metrics_df.write_parquet(ski_area_metrics_path)


def load_runs_pl() -> pl.DataFrame:
    path = get_runs_parquet_path()
    logging.info(f"Loading ski area metrics from {path}")
    return pl.read_parquet(source=path)


def load_ski_areas_pl() -> pl.DataFrame:
    path = get_ski_area_metrics_path()
    logging.info(f"Loading ski area metrics from {path}")
    return pl.read_parquet(source=path)


def load_bearing_distribution_pl() -> pl.DataFrame:
    """
    Table of ski area bearing distributions.
    Keyed on ski_area_id, num_bins, bin_center.
    """
    return (
        load_ski_areas_pl()
        .select("ski_area_id", "bearings")
        .explode("bearings")
        .unnest("bearings")
    )


def _get_bearing_summary_stats_pl(struct_series: pl.Series) -> BearingStatsModel:
    df = (
        struct_series.alias("input_struct")
        .to_frame()
        .unnest("input_struct")
        .drop_nulls()
    )
    try:
        (hemisphere,) = df.get_column("hemisphere").unique()
    except ValueError:
        hemisphere = None
    return get_bearing_summary_stats(
        bearings=df.get_column("bearing_mean").to_numpy(),
        net_magnitudes=df.get_column("bearing_magnitude_net").to_numpy(),
        cum_magnitudes=df.get_column("bearing_magnitude_cum").to_numpy(),
        hemisphere=hemisphere,
    )


def _prepare_ski_area_filters(
    ski_area_filters: list[pl.Expr] | None = None,
) -> list[pl.Expr]:
    if not ski_area_filters:
        ski_area_filters = [pl.lit(True)]
    return ski_area_filters


def aggregate_ski_areas_pl(
    group_by: list[str],
    ski_area_filters: list[pl.Expr] | None = None,
) -> pl.DataFrame:
    assert len(group_by) > 0
    ski_area_filters = _prepare_ski_area_filters(ski_area_filters)
    bearings_pl = (
        aggregate_ski_area_bearing_dists_pl(
            group_by=group_by, ski_area_filters=ski_area_filters
        )
        .group_by(*group_by)
        .agg(bearings=pl.struct(pl.exclude(group_by)))
    )
    return (
        load_ski_areas_pl()
        .filter(*ski_area_filters)
        .group_by(*group_by)
        .agg(
            ski_areas_count=pl.n_unique("ski_area_id"),
            country_count=pl.n_unique("country"),
            run_count_filtered=pl.sum("run_count_filtered"),
            latitude=pl.mean("latitude"),
            longitude=pl.mean("longitude"),
            combined_vertical=pl.sum("combined_vertical"),
            _bearing_stats=pl.struct(
                "bearing_mean",
                "bearing_alignment",
                "bearing_magnitude_net",
                "bearing_magnitude_cum",
                "hemisphere",
            ).map_batches(_get_bearing_summary_stats_pl, returns_scalar=True),
        )
        .unnest("_bearing_stats")
        .join(bearings_pl, on=group_by)
        .sort(*group_by)
    )


def aggregate_ski_area_bearing_dists_pl(
    group_by: list[str],
    ski_area_filters: list[pl.Expr] | None = None,
) -> pl.DataFrame:
    return (
        load_ski_areas_pl()
        .filter(*_prepare_ski_area_filters(ski_area_filters))
        .explode("bearings")
        .unnest("bearings")
        .group_by(*group_by, "num_bins", "bin_index")
        .agg(
            bin_center=pl.first("bin_center"),
            bin_count=pl.sum("bin_count"),
            bin_label=pl.first("bin_label"),
        )
        .with_columns(
            bin_proportion=pl.col("bin_count")
            / pl.sum("bin_count").over(*group_by, "num_bins"),
        )
        .sort(*group_by, "num_bins", "bin_center")
    )


def bearing_dists_by_us_state() -> pl.DataFrame:
    return aggregate_ski_areas_pl(
        group_by=["region"],
        ski_area_filters=[
            pl.col("country") == "United States",
            pl.col("region").is_not_null(),
            pl.col("status") == "operating",
            pl.col("ski_area_name").is_not_null(),
        ],
    )


def bearing_dists_by_hemisphere() -> pl.DataFrame:
    return aggregate_ski_areas_pl(
        group_by=["hemisphere"],
        ski_area_filters=[
            pl.col("hemisphere").is_not_null(),
            pl.col("status") == "operating",
            pl.col("ski_area_name").is_not_null(),
        ],
    )


def bearing_dists_by_status() -> pl.DataFrame:
    """
    Bearing distributions by ski area operating status.
    Only includes ski areas in the northern hemisphere because there were no abandoned ski areas in the southern hemisphere at ToW.
    """
    return aggregate_ski_areas_pl(
        group_by=["hemisphere", "status"],
        ski_area_filters=[
            pl.col("hemisphere") == "north",
            pl.col("status").is_in(["abandoned", "operating"]),
        ],
    ).with_columns(
        group_name=pl.format(
            "{} in {} Hem.",
            pl.col("status").str.to_titlecase(),
            pl.col("hemisphere").str.to_titlecase(),
        )
    )


def bearing_dists_by_country() -> pl.DataFrame:
    return aggregate_ski_areas_pl(
        group_by=["country"],
        ski_area_filters=[
            pl.col("country").is_not_null(),
            pl.col("status") == "operating",
            pl.col("ski_area_name").is_not_null(),
        ],
    )


def ski_rose_the_world(min_combined_vertical: int = 10_000) -> pl.DataFrame:
    path = get_data_directory().joinpath("ski-roses.pdf")
    pdf_pages = PdfPages(
        filename=path,
        metadata={
            "Title": "Ski Roses of the World: Downhill Ski Trail Orientations",
            "Author": "https://github.com/dhimmel/ski-bearing-stats",
        },
    )
    grouping_col_to_stats = {
        "hemisphere": bearing_dists_by_hemisphere(),
        "group_name": bearing_dists_by_status(),
        "country": bearing_dists_by_country(),
        "region": bearing_dists_by_us_state(),
    }
    figures = []
    for grouping_col, groups_pl in grouping_col_to_stats.items():
        logging.info(f"Plotting ski roses by {grouping_col}")
        groups_pl = groups_pl.filter(
            pl.col("combined_vertical") >= min_combined_vertical
        )
        if groups_pl.is_empty():
            logging.info(
                f"Skipping {grouping_col} plot which returns no groups with combined_vertical >= {min_combined_vertical:,}m."
            )
            continue
        fig = subplot_orientations(
            groups_pl=groups_pl,
            grouping_col=grouping_col,
            n_cols=min(4, len(groups_pl)),
            free_y=True,
        )
        figures.append(fig)
    logging.info(f"Writing ski rose the world to {path}")
    with pdf_pages:
        for fig in figures:
            pdf_pages.savefig(fig, facecolor="#FFFFFF", bbox_inches="tight")


def get_ski_area_frontend_table() -> pl.DataFrame:
    """
    Dataframe of ski areas and their metrics designed for display.
    """
    cardinal_direction_props = (
        load_bearing_distribution_pl()
        .filter(pl.col("num_bins") == 4)
        .pivot(on="bin_label", index="ski_area_id", values="bin_proportion")
        .select(
            "ski_area_id",
            pl.all().exclude("ski_area_id").round(5).name.prefix("bin_proportion_"),
        )
    )
    return (
        load_ski_areas_pl()
        .filter(pl.col("run_count_filtered") >= 3)
        .filter(pl.col("combined_vertical") >= 50)
        .filter(pl.col("ski_area_name").is_not_null())
        .select(
            "ski_area_id",
            "ski_area_name",
            pl.format(
                "<a href='https://openskimap.org/?obj={}' target='_blank'>{}</a>",
                "ski_area_id",
                "ski_area_name",
            ).alias("ski_area_hyper"),
            "status",
            "country",
            "region",
            "locality",
            "run_count_filtered",
            pl.col("combined_vertical").round(5),
            "bearing_mean",
            "bearing_alignment",
            "poleward_affinity",
            "eastward_affinity",
        )
        .join(cardinal_direction_props, on="ski_area_id", how="left")
        .sort("ski_area_name")
    )


def get_ski_area_reactable() -> reactable.Reactable:
    return reactable.Reactable(
        data=get_ski_area_frontend_table().drop("ski_area_id", "ski_area_name"),
        striped=True,
        searchable=True,
        columns=[
            reactable.Column(
                id="ski_area_hyper",
                name="Name",
                html=True,
                min_width=250,
                searchable=True,
            ),
            reactable.Column(
                id="status",
                name="Status",
                searchable=True,
            ),
            reactable.Column(
                id="country",
                name="Country",
                searchable=True,
            ),
            reactable.Column(
                id="region",
                name="Region",
                searchable=True,
            ),
            reactable.Column(
                id="locality",
                name="Locality",
                searchable=True,
            ),
            reactable.Column(
                id="run_count_filtered",
                name="Runs",
                searchable=False,
            ),
            reactable.Column(
                id="combined_vertical",
                name="Vertical",
                format=reactable.ColFormat(suffix="m", digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bearing_mean",
                name="Azimuth",
                format=reactable.ColFormat(suffix="°", digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bearing_alignment",
                name="Alignment",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="poleward_affinity",
                name="Poleward",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="eastward_affinity",
                name="Eastward",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bin_proportion_N",
                name="N",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bin_proportion_E",
                name="E",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bin_proportion_S",
                name="S",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
            reactable.Column(
                id="bin_proportion_W",
                name="W",
                format=reactable.ColFormat(percent=True, digits=0),
                searchable=False,
            ),
        ],
        column_groups=[
            reactable.ColGroup(name="Ski Area", columns=["ski_area_hyper", "status"]),
            reactable.ColGroup(
                name="Location",
                columns=["country", "region", "locality", "country_subdiv_code"],
            ),
            reactable.ColGroup(
                name="Downhill Runs",
                columns=["run_count_filtered", "combined_vertical"],
            ),
            reactable.ColGroup(
                name="Mean Bearing",
                columns=[
                    "bearing_mean",
                    "bearing_alignment",
                    "poleward_affinity",
                    "eastward_affinity",
                ],
            ),
            reactable.ColGroup(
                name="Proportion",
                columns=[
                    "bin_proportion_N",
                    "bin_proportion_E",
                    "bin_proportion_S",
                    "bin_proportion_W",
                ],
            ),
        ],
    )
