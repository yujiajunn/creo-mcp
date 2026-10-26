"""Pure tests for the cavity prism -> editable segment consolidation."""

from __future__ import annotations

import unittest

from creo_mcp.cavity_segments import _union_area, consolidate_prisms
from creo_mcp.errors import CreoMcpError

Prism = tuple[float, float, float, float, float, float]


def rect_stack(steps: list[tuple[float, float, float]]) -> list[Prism]:
    """Build one centred rectangular step per (half_size, z_bottom, z_top) entry."""
    prisms: list[Prism] = []
    for half_size, z_bottom, z_top in steps:
        prisms.append((-half_size, -half_size, half_size, half_size, z_bottom, z_top))
    return prisms


class ConsolidatePrismsTests(unittest.TestCase):
    def test_single_prism_produces_one_segment_without_waste(self) -> None:
        plan = consolidate_prisms([(-5.0, -5.0, 5.0, 5.0, 0.0, 10.0)], max_segments=6)

        self.assertEqual(len(plan.segments), 1)
        segment = plan.segments[0]
        self.assertEqual(segment.index, 1)
        self.assertAlmostEqual(segment.length_mm, 10.0)
        self.assertAlmostEqual(segment.width_mm, 10.0)
        self.assertAlmostEqual(segment.depth_mm, 10.0)
        self.assertAlmostEqual(plan.segment_volume_mm3, plan.reference_volume_mm3)
        self.assertAlmostEqual(plan.waste_volume_mm3, 0.0)
        self.assertEqual(plan.enlarged_segments, [])

    def test_planner_style_overlapping_prisms_never_report_negative_waste(self) -> None:
        # The real planner runs every rectangle up to the cavity mouth, so raw prism
        # volumes overlap. Waste is measured against the finest band staircase instead.
        prisms: list[Prism] = [
            (-20.0, -20.0, 20.0, 20.0, 5.0, 10.0),
            (-10.0, -10.0, 10.0, 10.0, 0.0, 10.0),
        ]
        plan = consolidate_prisms(prisms, max_segments=6)

        self.assertGreater(plan.prism_volume_sum_mm3, plan.reference_volume_mm3)
        self.assertGreaterEqual(plan.waste_volume_mm3, 0.0)
        self.assertGreaterEqual(plan.waste_ratio, 0.0)
        self.assertAlmostEqual(plan.reference_volume_mm3, 10000.0, places=3)

    def test_steep_steps_below_a_wider_mouth_are_left_untouched(self) -> None:
        plan = consolidate_prisms(
            rect_stack([(20.0, 5.0, 10.0), (10.0, 0.0, 5.0)]),
            max_segments=6,
        )

        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(plan.enlarged_segments, [])
        top, bottom = plan.segments
        self.assertGreater(top.length_mm, bottom.length_mm)

    def test_a_deeper_step_wider_than_the_mouth_is_closed_by_enlarging_upward(self) -> None:
        plan = consolidate_prisms(
            rect_stack([(10.0, 5.0, 10.0), (20.0, 0.0, 5.0)]),
            max_segments=6,
        )

        self.assertEqual(plan.enlarged_segments, [1])
        top = plan.segments[0]
        self.assertAlmostEqual(top.length_mm, 40.0)
        self.assertTrue(top.enlarged_for_insertion)
        # The whole stack must now admit a straight +Z draw.
        for deeper, shallower in zip(plan.segments[1:], plan.segments[:-1]):
            self.assertLessEqual(deeper.x_min_mm, shallower.x_min_mm + 1e-9)
            self.assertLessEqual(
                deeper.x_min_mm + deeper.length_mm,
                shallower.x_min_mm + shallower.length_mm + 1e-9,
            )

    def test_max_segments_cap_is_respected_and_still_conservative(self) -> None:
        prisms = rect_stack(
            [(20.0 - step, step * 2.0, step * 2.0 + 2.0) for step in range(10)]
        )
        plan = consolidate_prisms(prisms, max_segments=4)

        self.assertLessEqual(len(plan.segments), 4)
        self.assertGreaterEqual(plan.segment_volume_mm3, plan.reference_volume_mm3)
        self.assertGreaterEqual(plan.waste_ratio, 0.0)
        total_depth = sum(segment.depth_mm for segment in plan.segments)
        self.assertAlmostEqual(total_depth, 20.0, places=6)

    def test_footprints_within_tolerance_merge_into_one_segment(self) -> None:
        prisms: list[Prism] = [
            (-10.0, -10.0, 10.0, 10.0, 0.0, 5.0),
            (-10.0, -10.0, 10.0004, 10.0, 5.0, 10.0),
        ]
        plan = consolidate_prisms(prisms, max_segments=6, footprint_tolerance_mm=0.05)

        self.assertEqual(len(plan.segments), 1)
        self.assertEqual(plan.segments[0].merged_bands, 2)

    def test_footprints_beyond_tolerance_stay_separate(self) -> None:
        prisms: list[Prism] = [
            (-10.0, -10.0, 10.0, 10.0, 0.0, 5.0),
            (-10.5, -10.0, 10.0, 10.0, 5.0, 10.0),
        ]
        plan = consolidate_prisms(prisms, max_segments=6, footprint_tolerance_mm=0.05)

        self.assertEqual(len(plan.segments), 2)

    def test_segments_cover_the_full_cavity_depth(self) -> None:
        plan = consolidate_prisms(
            rect_stack([(20.0, 6.0, 10.0), (10.0, 0.0, 6.0)]),
            max_segments=6,
        )

        self.assertAlmostEqual(plan.cavity_bottom_mm, 0.0)
        self.assertAlmostEqual(plan.opening_top_mm, 10.0)
        self.assertAlmostEqual(plan.segments[-1].z_bottom_mm, 0.0)
        self.assertAlmostEqual(plan.segments[0].z_top_mm, 10.0)

    def test_rejects_empty_input_and_bad_segment_budget(self) -> None:
        with self.assertRaises(CreoMcpError):
            consolidate_prisms([])
        with self.assertRaises(CreoMcpError):
            consolidate_prisms(rect_stack([(5.0, 0.0, 1.0)]), max_segments=0)

    def test_side_by_side_devices_stay_separate_pockets(self) -> None:
        # Regression: an early version took one bounding rect per band, which swallowed
        # the air between two devices and inflated the segment table by ~60 %.
        prisms: list[Prism] = [
            (-10.0, -10.0, 10.0, 10.0, 0.0, 10.0),
            (100.0, 0.0, 120.0, 20.0, 0.0, 10.0),
        ]
        plan = consolidate_prisms(prisms, max_segments=6)

        self.assertEqual(plan.column_count, 2)
        self.assertEqual(len(plan.segments), 2)
        # 400 mm2 + 400 mm2 over 10 mm; a merged bounding box would have claimed 3900.
        self.assertAlmostEqual(plan.reference_volume_mm3, 8000.0, places=3)
        self.assertEqual(
            {segment.column_index for segment in plan.segments},
            {1, 2},
        )

    def test_segment_budget_is_met_without_cross_column_collapse_when_possible(self) -> None:
        deep = rect_stack([(20.0, 0.0, 2.0), (19.0, 2.0, 4.0), (18.0, 4.0, 10.0)])
        other: list[Prism] = [(100.0, 0.0, 120.0, 20.0, 0.0, 10.0)]
        plan = consolidate_prisms(deep + other, max_segments=2, min_step_depth_mm=1.5)

        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(plan.column_count, 2)
        self.assertGreaterEqual(plan.waste_volume_mm3, 0.0)
        self.assertAlmostEqual(
            sum(segment.depth_mm for segment in plan.segments if segment.column_index == 1),
            10.0,
            places=6,
        )

    def test_report_shape_is_json_friendly(self) -> None:
        plan = consolidate_prisms(rect_stack([(20.0, 5.0, 10.0), (10.0, 0.0, 5.0)]))
        report = plan.to_dict()

        self.assertEqual(report["segment_count"], len(plan.segments))
        self.assertIn("waste_ratio", report)
        self.assertEqual(set(report["segments"][0]), set(plan.segments[0].to_dict()))


    def test_min_step_depth_folds_a_thin_ledge_into_the_cheaper_neighbour(self) -> None:
        prisms = rect_stack([(10.0, 0.0, 5.0), (20.0, 5.0, 6.0), (15.0, 6.0, 10.0)])

        uncapped = consolidate_prisms(prisms, max_segments=6)
        folded = consolidate_prisms(prisms, max_segments=6, min_step_depth_mm=2.0)

        self.assertEqual(len(uncapped.segments), 3)
        self.assertEqual(len(folded.segments), 2)
        # Segments are described top-first, so the deeper step's top meets the mouth step.
        self.assertAlmostEqual(folded.segments[0].z_top_mm, 10.0, places=6)
        self.assertAlmostEqual(
            folded.segments[1].z_top_mm,
            folded.segments[0].z_bottom_mm,
            places=6,
        )
        self.assertAlmostEqual(folded.segments[1].z_bottom_mm, 0.0, places=6)
        self.assertGreaterEqual(folded.waste_volume_mm3, 0.0)
        self.assertEqual(folded.enlarged_segments, [])
        self.assertAlmostEqual(folded.reference_volume_mm3, 7200.0, places=3)
        self.assertAlmostEqual(folded.segment_volume_mm3, 10000.0, places=3)

    def test_min_step_depth_never_loses_the_wider_lower_footprint(self) -> None:
        # A thin but wider ledge must be carried by the merged step, not averaged away.
        prisms = rect_stack([(20.0, 0.0, 1.0), (10.0, 1.0, 10.0)])
        plan = consolidate_prisms(prisms, max_segments=6, min_step_depth_mm=2.0)

        self.assertEqual(len(plan.segments), 1)
        top = plan.segments[0]
        self.assertAlmostEqual(top.length_mm, 40.0)
        self.assertAlmostEqual(top.z_bottom_mm, 0.0)
        self.assertAlmostEqual(top.z_top_mm, 10.0)
        self.assertAlmostEqual(plan.reference_volume_mm3, 5200.0, places=3)
        self.assertAlmostEqual(plan.segment_volume_mm3, 16000.0, places=3)
        self.assertGreaterEqual(plan.waste_volume_mm3, 0.0)

    def test_negative_min_step_depth_is_rejected(self) -> None:
        with self.assertRaises(CreoMcpError):
            consolidate_prisms(rect_stack([(5.0, 0.0, 1.0)]), min_step_depth_mm=-1.0)


class UnionAreaTests(unittest.TestCase):
    """The over-cut baseline is only trustworthy if the sweep itself is exact."""

    def test_single_rectangle_is_its_own_area(self) -> None:
        self.assertAlmostEqual(_union_area([(0.0, 0.0, 10.0, 4.0)]), 40.0)

    def test_nested_rectangle_is_counted_once(self) -> None:
        self.assertAlmostEqual(
            _union_area([(0.0, 0.0, 10.0, 10.0), (2.0, 2.0, 8.0, 8.0)]),
            100.0,
        )

    def test_stacked_bars_sum_without_double_counting(self) -> None:
        self.assertAlmostEqual(
            _union_area([(0.0, 0.0, 10.0, 4.0), (0.0, 4.0, 10.0, 8.0)]),
            80.0,
        )

    def test_disjoint_rectangles_are_added(self) -> None:
        self.assertAlmostEqual(
            _union_area([(0.0, 0.0, 4.0, 4.0), (10.0, 10.0, 12.0, 12.0)]),
            20.0,
        )

    def test_l_shape_costs_less_than_its_bounding_box(self) -> None:
        # One 10x10 step would cut 100 mm2 where the real footprint is two bars = 80 mm2.
        prisms: list[Prism] = [
            (0.0, 0.0, 10.0, 4.0, 0.0, 10.0),
            (0.0, 4.0, 4.0, 10.0, 0.0, 10.0),
        ]
        plan = consolidate_prisms(prisms, max_segments=6)

        self.assertAlmostEqual(plan.union_volume_mm3, 640.0, places=3)
        self.assertGreater(plan.segment_volume_mm3, plan.union_volume_mm3)
        self.assertAlmostEqual(plan.waste_ratio, 1000.0 / 640.0 - 1.0, places=6)
        self.assertEqual(plan.column_count, 1)


class PerDeviceOwnerTests(unittest.TestCase):
    """A tray holding several devices must cut several pockets, never one bounding box."""

    def setUp(self) -> None:
        # Device A 20x20, device B 20x40; their footprints touch, so a shared
        # clustering pass would merge them into one 40x40 pocket.
        self.prisms: list[Prism] = [
            (-10.0, -10.0, 10.0, 10.0, 0.0, 10.0),
            (10.0, -20.0, 30.0, 20.0, 0.0, 10.0),
        ]

    def test_touching_devices_stay_two_pockets_when_owners_differ(self) -> None:
        merged = consolidate_prisms(self.prisms, max_segments=6)
        split = consolidate_prisms(self.prisms, max_segments=6, owners=[0, 1])

        self.assertEqual(merged.column_count, 1)
        self.assertEqual(merged.device_count, 1)
        self.assertEqual(split.column_count, 2)
        self.assertEqual(split.device_count, 2)
        self.assertEqual(sorted(segment.device_index for segment in split.segments), [1, 2])
        # Both cuts contain the same true cavity; only the split keeps the extra air out.
        self.assertAlmostEqual(split.union_volume_mm3, merged.union_volume_mm3, places=3)
        self.assertAlmostEqual(split.segment_volume_mm3, 12000.0, places=3)
        self.assertAlmostEqual(merged.segment_volume_mm3, 16000.0, places=3)
        self.assertLess(split.waste_ratio, merged.waste_ratio)

    def test_owners_must_match_the_prism_count(self) -> None:
        with self.assertRaises(CreoMcpError):
            consolidate_prisms(self.prisms, owners=[0])

    def test_single_owner_reproduces_unowned_behaviour(self) -> None:
        unowned = consolidate_prisms(self.prisms)
        owned = consolidate_prisms(self.prisms, owners=[0, 0])

        self.assertEqual(len(unowned.segments), len(owned.segments))
        self.assertEqual(unowned.column_count, owned.column_count)
        self.assertEqual(
            [segment.to_dict() for segment in unowned.segments],
            [segment.to_dict() for segment in owned.segments],
        )


if __name__ == "__main__":
    unittest.main()
