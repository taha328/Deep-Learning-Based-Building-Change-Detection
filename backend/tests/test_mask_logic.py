import numpy as np

from src.domain.inference import derive_new_building_products


def test_new_building_mask_logic_matches_notebook() -> None:
    change_prob = np.array([[0.9, 0.9], [0.4, 0.9]], dtype=np.float32)
    t1_prob = np.array([[0.1, 0.8], [0.1, 0.2]], dtype=np.float32)
    t2_prob = np.array([[0.8, 0.9], [0.9, 0.1]], dtype=np.float32)

    products = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=0,
    )

    expected = np.array([[True, False], [False, False]])
    assert np.array_equal(products["new_building_mask"], expected)


def test_old_building_mask_dilation_suppresses_edge_slivers() -> None:
    t1_prob = np.zeros((5, 5), dtype=np.float32)
    t2_prob = np.zeros((5, 5), dtype=np.float32)
    change_prob = np.ones((5, 5), dtype=np.float32)

    t1_prob[1:4, 1:4] = 1.0
    t2_prob[0:4, 1:4] = 1.0

    without_dilation = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=0,
    )
    with_dilation = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=1,
    )

    expected_sliver = np.zeros((5, 5), dtype=bool)
    expected_sliver[0, 1:4] = True

    assert np.array_equal(without_dilation["new_building_mask"], expected_sliver)
    assert not with_dilation["new_building_mask"].any()


def test_new_building_core_distance_suppresses_shifted_old_building_rims() -> None:
    t1_prob = np.zeros((9, 9), dtype=np.float32)
    t2_prob = np.zeros((9, 9), dtype=np.float32)
    change_prob = np.ones((9, 9), dtype=np.float32)

    t1_prob[2:7, 2:5] = 1.0
    t2_prob[2:7, 5:8] = 1.0

    without_core_filter = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=2,
        new_building_core_distance_pixels=0,
    )
    with_core_filter = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=2,
        new_building_core_distance_pixels=2,
    )

    expected_rim = np.zeros((9, 9), dtype=bool)
    expected_rim[2:7, 7] = True

    assert np.array_equal(without_core_filter["new_building_mask_filtered"], expected_rim)
    assert not with_core_filter["new_building_mask_filtered"].any()
    assert not with_core_filter["new_building_mask"].any()


def test_new_building_core_distance_keeps_real_buildings_with_interior_core() -> None:
    t1_prob = np.zeros((11, 11), dtype=np.float32)
    t2_prob = np.zeros((11, 11), dtype=np.float32)
    change_prob = np.ones((11, 11), dtype=np.float32)

    t1_prob[3:8, 1:4] = 1.0
    t2_prob[3:8, 1:4] = 1.0
    t2_prob[3:8, 6:10] = 1.0

    products = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=2,
        new_building_core_distance_pixels=2,
    )

    expected = np.zeros((11, 11), dtype=bool)
    expected[3:8, 6:10] = True

    assert np.array_equal(products["new_building_mask_filtered"], expected)
    assert np.array_equal(products["new_building_mask"], expected)
