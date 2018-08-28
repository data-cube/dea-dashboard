import json
import re
from pathlib import Path

import pytest
from dateutil import tz
from flask import Response
from flask.testing import FlaskClient

import cubedash
from cubedash import _model
from cubedash.summary import SummaryStore
from datacube.index.hl import Doc2Dataset
from datacube.utils import read_documents
from requests_html import HTML

TEST_DATA_DIR = Path(__file__).parent / 'data'

DEFAULT_TZ = tz.gettz('Australia/Darwin')


def _populate_from_dump(session_dea_index, expected_type: str, dump_path: Path):
    ls8_nbar_scene = session_dea_index.products.get_by_name(expected_type)
    dataset_count = 0

    create_dataset = Doc2Dataset(session_dea_index)

    for _, doc in read_documents(dump_path):
        label = doc['ga_label'] if ('ga_label' in doc) else doc['id']
        dataset, err = create_dataset(doc, f"file://example.com/test_dataset/{label}")
        assert dataset is not None, err
        created = session_dea_index.datasets.add(dataset)

        assert created.type.name == ls8_nbar_scene.name
        dataset_count += 1

    print(f"Populated {dataset_count} of {expected_type}")
    return dataset_count


@pytest.fixture(scope='module', autouse=True)
def populate_index(module_dea_index):
    """
    Index populated with example datasets. Assumes our tests wont modify the data!

    It's module-scoped as it's expensive to populate.
    """
    _populate_from_dump(
        module_dea_index,
        'wofs_albers',
        TEST_DATA_DIR / 'wofs-albers-sample.yaml.gz'
    )
    _populate_from_dump(
        module_dea_index,
        'high_tide_comp_20p',
        TEST_DATA_DIR / 'high_tide_comp_20p.yaml.gz'
    )
    return module_dea_index


@pytest.fixture(scope='function')
def cubedash_client(summary_store: SummaryStore) -> FlaskClient:
    _model.STORE = summary_store
    _model.STORE.refresh_all_products()
    for product in summary_store.index.products.get_all():
        _model.STORE.get_or_update(product.name)

    cubedash.app.config['TESTING'] = True
    return cubedash.app.test_client()


def test_default_redirect(cubedash_client: FlaskClient):
    client = cubedash_client
    rv: Response = client.get('/', follow_redirects=False)
    # Redirect to a default.
    assert rv.location.endswith('/ls7_nbar_scene')


def test_get_overview(cubedash_client: FlaskClient):
    client = cubedash_client

    rv: Response = client.get('/wofs_albers')
    assert b'11 datasets' in rv.data
    i = rv.data.find(b'Last processed <time')
    print(rv.data[i:i + 62])
    assert b'Last processed <time datetime=2018-05-20T11:25:35' in rv.data
    assert b'Historic Flood Mapping Water Observations from Space' in rv.data

    rv: Response = client.get('/wofs_albers/2017')

    assert b'11 datasets' in rv.data
    assert b'Last processed <time datetime=2018-05-20T11:25:35' in rv.data
    assert b'Historic Flood Mapping Water Observations from Space' in rv.data

    rv: Response = client.get('/wofs_albers/2017/04')
    assert b'4 datasets' in rv.data
    assert b'Last processed <time datetime=2018-05-20T09:36:57' in rv.data
    assert b'Historic Flood Mapping Water Observations from Space' in rv.data


def test_view_dataset(cubedash_client: FlaskClient):

    # ls7_level1_scene dataset
    rv: Response = cubedash_client.get("/dataset/57848615-2421-4d25-bfef-73f57de0574d")
    # Label of dataset is header
    assert b'<h2>LS7_ETM_OTH_P51_GALPGS01-002_105_074_20170501</h2>' in rv.data

    # wofs_albers dataset (has no label or location)
    rv: Response = cubedash_client.get("/dataset/20c024b5-6623-4b06-b00c-6b5789f81eeb")
    assert b'-20.502 to -19.6' in rv.data
    assert b'132.0 to 132.924' in rv.data


def test_view_product(cubedash_client: FlaskClient):
    rv: Response = cubedash_client.get("/product/ls7_nbar_scene")
    assert b'Landsat 7 NBAR 25 metre' in rv.data


def test_about_page(cubedash_client: FlaskClient):
    rv: Response = cubedash_client.get("/about")
    assert b"wofs_albers" in rv.data
    assert b'11 total datasets' in rv.data


@pytest.mark.skip(reason="TODO: fix out-of-date range return value")
def test_out_of_date_range(cubedash_client: FlaskClient):
    """
    We have generated summaries for this product, but the date is out of the product's date range.
    """
    client = cubedash_client

    rv: Response = client.get('/wofs_albers/2010')
    assert rv.status_code == 200
    print(rv.data.decode('utf-8'))
    # The common error here is to say "No data: not yet generated" rather than "0 datasets"
    assert b'0 datasets' in rv.data
    assert b'Historic Flood Mapping Water Observations from Space' in rv.data


def test_loading_high_low_tid(cubedash_client: FlaskClient):
    rv: Response = cubedash_client.get("/high_tide_comp_20p/2008")
    assert rv.status_code == 200
    html = HTML(html=rv.data.decode('utf-8'))

    assert html.search('High Tide 20 percentage composites for entire coastline') is not None

    assert '306 datasets' in html.find('.dataset-count', first=True).text
    # Footprint is not exact due to shapely.simplify()
    assert re.match(r'2,984,...km2 \(approx', html.find('.coverage-footprint-area', first=True).text)

    assert html.find('.last-processed time', first=True).attrs['datetime'] == '2017-06-08T20:58:07.014314+00:00'


def test_api_returns_high_tide_comp_datasets(cubedash_client: FlaskClient):
    """
    These are slightly fun to handle as they are a small number with a huge time range.
    """
    geojson = _get_geojson(cubedash_client, '/api/datasets/high_tide_comp_20p')
    assert len(geojson['features']) == 306, "Not all high tide datasets returned as geojson"

    # Check that they're not just using the center time.
    # Within the time range, but not the center_time.
    # Range: '2000-01-01T00:00:00' to '2016-10-31T00:00:00'
    # year
    geojson = _get_geojson(cubedash_client, '/api/datasets/high_tide_comp_20p/2000')
    assert len(geojson['features']) == 306, "Expected high tide datasets within whole dataset range"
    # month
    geojson = _get_geojson(cubedash_client, '/api/datasets/high_tide_comp_20p/2009/5')
    assert len(geojson['features']) == 306, "Expected high tide datasets within whole dataset range"
    # day
    geojson = _get_geojson(cubedash_client, '/api/datasets/high_tide_comp_20p/2016/10/1')
    assert len(geojson['features']) == 306, "Expected high tide datasets within whole dataset range"

    # Completely out of the test dataset time range. No results.
    geojson = _get_geojson(cubedash_client, '/api/datasets/high_tide_comp_20p/2018')
    assert len(geojson['features']) == 0, "Expected no high tide datasets in in this year"


def test_api_returns_scenes_as_geojson(cubedash_client: FlaskClient):
    """
    L1 scenes have no footprint, falls back to bounds. Have weird CRSes too.
    """
    geojson = _get_geojson(cubedash_client, '/api/datasets/ls8_level1_scene')
    assert len(geojson['features']) == 7, "Unexpected scene polygon count"


def test_api_returns_tiles_as_geojson(cubedash_client: FlaskClient):
    """
    Covers most of the 'normal' products: they have a footprint, bounds and a simple crs epsg code.
    """
    geojson = _get_geojson(cubedash_client, '/api/datasets/ls7_nbart_albers')
    assert len(geojson['features']) == 4, "Unepected albers polygon count"


def test_api_returns_high_tide_comp_regions(cubedash_client: FlaskClient):
    """
    High tide doesn't have anything we can use as regions.

    It should be empty (no regions supported) rather than throw an exception.
    """
    geojson = _get_geojson(cubedash_client, '/api/regions/high_tide_comp_20p')
    assert geojson == None


def test_api_returns_scene_regions(cubedash_client: FlaskClient):
    """
    L1 scenes have no footprint, falls back to bounds. Have weird CRSes too.
    """
    geojson = _get_geojson(cubedash_client, '/api/regions/ls8_level1_scene')
    assert len(geojson['features']) == 7, "Unexpected scene region count"


def test_api_returns_tiles_regions(cubedash_client: FlaskClient):
    """
    Covers most of the 'normal' products: they have a footprint, bounds and a simple crs epsg code.
    """
    geojson = _get_geojson(cubedash_client, '/api/regions/ls7_nbart_albers')
    assert len(geojson['features']) == 4, "Unexpected albers region count"


def test_api_returns_limited_tile_regions(cubedash_client: FlaskClient):
    """
    Covers most of the 'normal' products: they have a footprint, bounds and a simple crs epsg code.
    """
    geojson = _get_geojson(cubedash_client, '/api/regions/wofs_albers/2017/04')
    assert len(geojson['features']) == 4, "Unexpected wofs albers region month count"
    geojson = _get_geojson(cubedash_client, '/api/regions/wofs_albers/2017/04/20')
    print(json.dumps(geojson, indent=4))
    assert len(geojson['features']) == 1, "Unexpected wofs albers region day count"
    geojson = _get_geojson(cubedash_client, '/api/regions/wofs_albers/2017/04/6')
    assert geojson is None, "Unexpected wofs albers region count"


def _get_geojson(cubedash_client, url):
    rv: Response = cubedash_client.get(url)
    assert rv.status_code == 200
    response_geojson = json.loads(rv.data)
    return response_geojson


def test_no_data_pages(cubedash_client: FlaskClient):
    """
    Fetch products that exist but have no summaries generated.

    (these should load with "empty" messages: not throw exceptions)
    """
    client = cubedash_client

    rv: Response = client.get('/ls8_nbar_albers/2017')
    assert rv.status_code == 200
    assert b'No data: not yet generated' in rv.data
    assert b'Unknown number of datasets' in rv.data

    rv: Response = client.get('/ls8_nbar_albers/2017/5')
    assert rv.status_code == 200
    assert b'No data: not yet generated' in rv.data
    assert b'Unknown number of datasets' in rv.data

    # Days are generated on demand: it should query and see that there are no datasets.
    rv: Response = client.get('/ls8_nbar_albers/2017/5/2')
    assert rv.status_code == 200
    assert b'0 datasets' in rv.data


def test_missing_dataset(cubedash_client: FlaskClient):
    rv: Response = cubedash_client.get('/datasets/f22a33f4-42f2-4aa5-9b20-cee4ca4a875c')
    assert rv.status_code == 404


def test_invalid_product(cubedash_client: FlaskClient):
    rv: Response = cubedash_client.get('/fake_test_product/2017')
    assert rv.status_code == 404