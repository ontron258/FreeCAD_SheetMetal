# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest

import FreeCAD as App
import Part
import Sketcher  # noqa: F401 - registers Sketcher document object types

from SheetMetalBoltConnectionCmd import (
    add_connection_participants,
    connection_cuts,
    create_bolted_connection,
)
from SheetMetalConnectedPatternCmd import (
    SMConnectedPartPattern,
    SMConnectedPatternCut,
    create_connected_part_pattern,
    linear_direction_definition,
    polar_axis_definition,
    pattern_transforms,
    migrate_connected_part_patterns,
    set_parent_pattern,
    sync_connection_participant_patterns,
)
from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties


def _sheet_part(doc, name, shape, placement=None):
    part = doc.addObject("App::Part", name)
    part.Label = name
    addSheetMetalPartProperties(part)
    if placement is not None:
        part.Placement = placement
    base = doc.addObject("Part::FeaturePython", name + "Base")
    base.Shape = shape
    part.addObject(base)
    part.Tip = base.Name
    return part, base


def _configured_polar_pattern(doc):
    source_shape = Part.makeBox(
        6.0, 6.0, 2.0, App.Vector(-3.0, -3.0, 0.0)
    )
    source_placement = App.Placement(
        App.Vector(10.0, 0.0, 0.0), App.Rotation()
    )
    source, _source_base = _sheet_part(
        doc, "RepeatedPart", source_shape, source_placement
    )
    fixed_shape = Part.makeBox(
        30.0, 30.0, 2.0, App.Vector(-15.0, -15.0, 0.0)
    )
    fixed_placement = App.Placement(
        App.Vector(0.0, 0.0, 6.0), App.Rotation()
    )
    fixed, _fixed_base = _sheet_part(
        doc, "FixedPart", fixed_shape, fixed_placement
    )
    sketch = doc.addObject("Sketcher::SketchObject", "SeedBoltLocator")
    sketch.addGeometry(Part.Point(App.Vector(10.0, 0.0, 0.0)), False)
    connection, seed_cuts = create_bolted_connection(
        doc, [(sketch, [])], [source, fixed]
    )
    pattern, instance_link, cuts = create_connected_part_pattern(
        doc, source, [connection], "Polar"
    )
    assert cuts == []
    pattern.PolarCenter = App.Vector(0.0, 0.0, 0.0)
    pattern.PolarAxis = App.Vector(0.0, 0.0, 1.0)
    pattern.TotalAngle = 360.0
    pattern.Closed = True
    return source, fixed, connection, pattern, instance_link, seed_cuts[1]


class TestConnectedPattern(unittest.TestCase):
    def test_legacy_pattern_cut_nodes_are_removed_and_links_rewired(self):
        doc = App.newDocument("ConnectedPatternLegacyMigration")
        try:
            source, _source_base = _sheet_part(
                doc,
                "LegacySource",
                Part.makeBox(4.0, 4.0, 2.0, App.Vector(-2.0, -2.0, 0.0)),
            )
            target, _target_base = _sheet_part(
                doc,
                "LegacyTarget",
                Part.makeBox(30.0, 10.0, 2.0, App.Vector(-5.0, -5.0, 0.0)),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            sketch = doc.addObject("Sketcher::SketchObject", "LegacyLocator")
            sketch.addGeometry(Part.Point(App.Vector(0.0, 0.0, 0.0)), False)
            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [source, target]
            )
            pattern, _link, _new_cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear"
            )
            pattern.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            pattern.Occurrences = 2
            pattern.Spacing = 10.0

            direct_cut = cuts[1]
            legacy = doc.addObject("Part::FeaturePython", "LegacyPatternCut")
            target.addObject(legacy)
            SMConnectedPatternCut(
                legacy, pattern, direct_cut, [direct_cut], target
            )
            target.Tip = legacy.Name
            pattern.addProperty(
                "App::PropertyStringList", "PatternCutNames", "Legacy"
            )
            pattern.PatternCutNames = [legacy.Name]
            follower = doc.addObject("Part::FeaturePython", "LegacyFollower")
            follower.addProperty("App::PropertyLink", "FollowedTip")
            follower.FollowedTip = legacy
            doc.recompute()
            legacy_name = legacy.Name

            removed = migrate_connected_part_patterns(doc)

            self.assertEqual(removed, 1)
            self.assertIsNone(doc.getObject(legacy_name))
            self.assertEqual(target.Tip, direct_cut.Name)
            self.assertIs(follower.FollowedTip, direct_cut)
            self.assertNotIn("PatternCutNames", pattern.PropertiesList)
            self.assertEqual(direct_cut.LastError, "")
            self.assertTrue(direct_cut.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_sibling_patterns_share_one_deduplicated_connection_cut_path(self):
        doc = App.newDocument("ConnectedPatternSiblingProviders")
        try:
            source, _source_base = _sheet_part(
                doc,
                "ReusableBracket",
                Part.makeBox(4.0, 4.0, 2.0, App.Vector(-2.0, -2.0, 0.0)),
            )
            target, _target_base = _sheet_part(
                doc,
                "SharedPanel",
                Part.makeBox(70.0, 10.0, 2.0, App.Vector(-5.0, -5.0, 0.0)),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            sketch = doc.addObject("Sketcher::SketchObject", "BracketLocator")
            sketch.addGeometry(Part.Point(App.Vector(0.0, 0.0, 0.0)), False)
            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [source, target]
            )
            parent, _parent_link, parent_cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear"
            )
            parent.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            parent.Occurrences = 2
            parent.Spacing = 10.0
            branch_a, _branch_a_link, branch_a_cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear", parent_pattern=parent
            )
            branch_a.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            branch_a.Occurrences = 2
            branch_a.Spacing = 20.0
            branch_b, _branch_b_link, branch_b_cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear", parent_pattern=parent
            )
            branch_b.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            branch_b.Occurrences = 2
            branch_b.Spacing = 40.0
            doc.recompute()

            self.assertEqual(parent_cuts, [])
            self.assertEqual(branch_a_cuts, [])
            self.assertEqual(branch_b_cuts, [])
            self.assertEqual(
                list(connection.OccurrenceProviders), [branch_a, branch_b]
            )
            self.assertEqual(connection.LocatorCount, 6)
            self.assertNotIn("Connections", parent.PropertiesList)
            self.assertEqual(parent.ConnectionNames, [connection.Name])
            self.assertEqual(
                [
                    obj
                    for obj in doc.Objects
                    if getattr(obj, "SheetMetalType", "")
                    == "ConnectedPatternCut"
                ],
                [],
            )
            target_cut = cuts[1]
            diameter = 0.344 * 25.4
            expected_removed = 6.0 * math.pi * (diameter * 0.5) ** 2 * 2.0
            self.assertAlmostEqual(
                target_cut.RemovedVolume.Value, expected_removed, places=4
            )
            self.assertEqual(target_cut.AggregateContributorCount, 1)
            self.assertEqual(target_cut.LastError, "")
        finally:
            App.closeDocument(doc.Name)

    def test_add_participant_extends_existing_pattern_chain(self):
        doc = App.newDocument("ConnectedPatternAddParticipant")
        try:
            source, _source_base = _sheet_part(
                doc,
                "RepeatedFoot",
                Part.makeBox(6.0, 6.0, 2.0, App.Vector(-3.0, -3.0, 0.0)),
            )
            outer, _outer_base = _sheet_part(
                doc,
                "OuterPanel",
                Part.makeBox(6.0, 6.0, 2.0, App.Vector(-3.0, -3.0, 0.0)),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            inner, inner_base = _sheet_part(
                doc,
                "InnerPanel",
                Part.makeBox(6.0, 6.0, 2.0, App.Vector(7.0, -3.0, 0.0)),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            sketch = doc.addObject("Sketcher::SketchObject", "FootLocator")
            sketch.addGeometry(Part.Point(App.Vector(0.0, 0.0, 0.0)), False)
            connection, _seed_cuts = create_bolted_connection(
                doc, [(sketch, [])], [source, outer]
            )
            parent, _parent_link, _parent_cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear"
            )
            parent.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            parent.Occurrences = 2
            parent.Spacing = 10.0
            child, _child_link, _child_cuts = create_connected_part_pattern(
                doc,
                source,
                [connection],
                "Linear",
                parent_pattern=parent,
            )
            child.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            child.Occurrences = 2
            child.Spacing = 20.0
            doc.recompute()

            added = add_connection_participants(connection, [inner])
            patterned = sync_connection_participant_patterns(connection, added)
            doc.recompute()

            self.assertEqual(len(added), 1)
            self.assertEqual(added[0].LocatorKeys, [])
            self.assertEqual(patterned, [])
            self.assertIn(inner.Name, connection.ParticipantNames)
            self.assertEqual(inner.Tip, added[0].Name)
            self.assertEqual(str(added[0].OccurrenceScope), "All Occurrences")
            self.assertGreater(added[0].RemovedVolume.Value, 0.0)
            self.assertLess(added[0].Shape.Volume, inner_base.Shape.Volume)
            self.assertEqual(added[0].AggregateContributorCount, 1)
            self.assertEqual(added[0].LastError, "")
        finally:
            App.closeDocument(doc.Name)

    def test_pattern_cuts_future_target_and_skips_non_intersections(self):
        doc = App.newDocument("ConnectedPatternSelectiveTargets")
        try:
            source, _source_base = _sheet_part(
                doc,
                "RepeatedFoot",
                Part.makeBox(
                    6.0, 6.0, 2.0, App.Vector(-3.0, -3.0, 0.0)
                ),
            )
            target, _target_base = _sheet_part(
                doc,
                "FuturePanel",
                Part.makeBox(
                    6.0, 6.0, 2.0, App.Vector(7.0, -3.0, 0.0)
                ),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            missed, _missed_base = _sheet_part(
                doc,
                "MissedPanel",
                Part.makeBox(
                    6.0, 6.0, 2.0, App.Vector(97.0, -3.0, 0.0)
                ),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            sketch = doc.addObject("Sketcher::SketchObject", "FootLocator")
            sketch.addGeometry(Part.Point(App.Vector(0.0, 0.0, 0.0)), False)
            connection, seed_cuts = create_bolted_connection(
                doc, [(sketch, [])], [source, target, missed]
            )
            self.assertEqual(seed_cuts[1].LocatorKeys, [])
            self.assertEqual(seed_cuts[2].LocatorKeys, [])

            pattern, _link, cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear"
            )
            self.assertEqual(cuts, [])
            pattern.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            pattern.Occurrences = 2
            pattern.Spacing = 10.0
            doc.recompute()

            target_cut = seed_cuts[1]
            missed_cut = seed_cuts[2]
            self.assertGreater(target_cut.RemovedVolume.Value, 0.0)
            self.assertEqual(target_cut.LastError, "")
            self.assertAlmostEqual(missed_cut.RemovedVolume.Value, 0.0)
            self.assertEqual(missed_cut.SkippedLocationCount, 2)
            self.assertEqual(missed_cut.LastError, "")
        finally:
            App.closeDocument(doc.Name)

    def test_panel_only_adjustment_locator_follows_source_pattern(self):
        doc = App.newDocument("ConnectedPatternPanelOnlyLocator")
        try:
            source, _source_base = _sheet_part(
                doc,
                "RepeatedFoot",
                Part.makeBox(
                    30.0, 20.0, 2.0, App.Vector(-5.0, -10.0, 0.0)
                ),
            )
            target, _target_base = _sheet_part(
                doc,
                "Panel",
                Part.makeBox(
                    30.0, 20.0, 2.0, App.Vector(45.0, -10.0, 0.0)
                ),
                App.Placement(App.Vector(0.0, 0.0, 6.0), App.Rotation()),
            )
            sketch = doc.addObject("Sketcher::SketchObject", "FootLocators")
            sketch.addGeometry(Part.Point(App.Vector(0.0, 0.0, 0.0)), False)
            sketch.addGeometry(Part.Point(App.Vector(10.0, 0.0, 0.0)), False)
            connection, seed_cuts = create_bolted_connection(
                doc, [(sketch, [])], [source, target]
            )
            source_cut, target_cut = seed_cuts
            source_cut.UseAllLocators = False
            source_cut.LocatorKeys = ["{}:0".format(sketch.Name)]
            target_cut.UseAllLocators = False
            target_cut.LocatorKeys = []
            connection.HoleOnlyLocatorKeys = ["{}:1".format(sketch.Name)]

            pattern, _link, cuts = create_connected_part_pattern(
                doc, source, [connection], "Linear"
            )
            self.assertEqual(cuts, [])
            pattern.LinearDirection = App.Vector(1.0, 0.0, 0.0)
            pattern.Occurrences = 2
            pattern.Spacing = 50.0
            doc.recompute()

            panel_cut = target_cut
            self.assertEqual(connection.BoltCount, 2)
            self.assertEqual(connection.AuxiliaryHoleCount, 2)
            self.assertEqual(source_cut.LastError, "")
            self.assertGreater(source_cut.RemovedVolume.Value, 0.0)
            self.assertEqual(panel_cut.SkippedLocationCount, 2)
            self.assertEqual(panel_cut.LastError, "")
            self.assertGreater(
                panel_cut.RemovedVolume.Value,
                1.5 * source_cut.RemovedVolume.Value,
            )
        finally:
            App.closeDocument(doc.Name)

    def test_follow_on_linear_pattern_repeats_parent_seed_set(self):
        doc = App.newDocument("ConnectedFollowOnPattern")
        try:
            source, _fixed, connection, parent, _parent_link, parent_cut = (
                _configured_polar_pattern(doc)
            )
            parent.Occurrences = 2
            coordinate_system = doc.addObject(
                "Part::LocalCoordinateSystem", "PatternCoordinates"
            )
            y_axis = coordinate_system.OriginFeatures[1]
            child, child_link, child_cuts = create_connected_part_pattern(
                doc,
                source,
                [connection],
                "Linear",
                direction_reference=y_axis,
                parent_pattern=parent,
            )
            child.Occurrences = 3
            child.Spacing = 5.0
            doc.recompute()

            self.assertTrue(
                linear_direction_definition(child).isEqual(
                    App.Vector(0.0, 1.0, 0.0), 1e-9
                )
            )
            self.assertEqual(len(pattern_transforms(parent, include_seed=True)), 2)
            self.assertEqual(len(pattern_transforms(child, include_seed=True)), 6)
            self.assertEqual(len(pattern_transforms(child)), 4)
            self.assertEqual(child.AdditionalOccurrences, 4)
            self.assertEqual(child_link.ElementCount, 4)
            self.assertEqual(child_cuts, [])
            self.assertEqual(list(connection.OccurrenceProviders), [child])
            self.assertEqual(str(parent_cut.OccurrenceScope), "All Occurrences")
            self.assertEqual(parent_cut.LastError, "")

            set_parent_pattern(child, None)
            doc.recompute()
            self.assertEqual(len(pattern_transforms(child, include_seed=True)), 3)
            self.assertEqual(child_link.ElementCount, 2)
            set_parent_pattern(child, parent)
            doc.recompute()
            self.assertEqual(len(pattern_transforms(child, include_seed=True)), 6)
            self.assertEqual(child_link.ElementCount, 4)
            with self.assertRaisesRegex(ValueError, "dependency cycle"):
                set_parent_pattern(parent, child)

            centers = [
                element.Shape.BoundBox.Center for element in child_link.ElementList
            ]
            expected = [
                App.Vector(10.0, 5.0, 1.0),
                App.Vector(-10.0, 5.0, 1.0),
                App.Vector(10.0, 10.0, 1.0),
                App.Vector(-10.0, 10.0, 1.0),
            ]
            for expected_center in expected:
                self.assertTrue(
                    any(center.isEqual(expected_center, 1e-7) for center in centers)
                )

            configuration = doc.addObject("App::FeaturePython", "Configuration")
            configuration.addProperty("App::PropertyInteger", "Tiers")
            configuration.addProperty("App::PropertyLength", "Step")
            configuration.Tiers = 2
            configuration.Step = 7.5
            child.setExpression("Occurrences", "Configuration.Tiers")
            child.setExpression("Spacing", "Configuration.Step")
            doc.recompute()
            self.assertEqual(int(child.Occurrences), 2)
            self.assertAlmostEqual(child.Spacing.Value, 7.5)
            self.assertEqual(child_link.ElementCount, 2)
        finally:
            App.closeDocument(doc.Name)

    def test_datum_line_drives_live_world_space_polar_axis(self):
        doc = App.newDocument("ConnectedPatternDatumAxis")
        try:
            container = doc.addObject("App::Part", "AxisContainer")
            container.Placement = App.Placement(
                App.Vector(5.0, 7.0, 11.0), App.Rotation()
            )
            datum = doc.addObject("Part::DatumLine", "RotaryAxis")
            container.addObject(datum)
            datum.Placement = App.Placement(
                App.Vector(2.0, 3.0, 4.0),
                App.Rotation(App.Vector(0.0, 1.0, 0.0), 90.0),
            )

            pattern = doc.addObject("App::FeaturePython", "Pattern")
            SMConnectedPartPattern(pattern)
            pattern.AxisReference = (datum, [""])
            pattern.Occurrences = 2
            pattern.TotalAngle = 360.0
            pattern.Closed = True
            doc.recompute()

            center, axis = polar_axis_definition(pattern)
            self.assertTrue(center.isEqual(App.Vector(7.0, 10.0, 15.0), 1e-9))
            self.assertTrue(axis.isEqual(App.Vector(1.0, 0.0, 0.0), 1e-9))
            transform = pattern_transforms(pattern)[0]
            rotated = transform.multVec(center + App.Vector(0.0, 1.0, 0.0))
            self.assertTrue(
                rotated.isEqual(center + App.Vector(0.0, -1.0, 0.0), 1e-7)
            )

            datum.Placement.Base = App.Vector(4.0, 3.0, 4.0)
            doc.recompute()
            moved_center, moved_axis = polar_axis_definition(pattern)
            self.assertTrue(
                moved_center.isEqual(App.Vector(9.0, 10.0, 15.0), 1e-9)
            )
            self.assertTrue(moved_axis.isEqual(axis, 1e-9))
        finally:
            App.closeDocument(doc.Name)

    def test_linear_and_polar_transform_definitions(self):
        doc = App.newDocument("ConnectedPatternTransforms")
        try:
            pattern = doc.addObject("App::FeaturePython", "Pattern")
            SMConnectedPartPattern(pattern)
            pattern.PatternType = "Linear"
            pattern.Occurrences = 3
            pattern.LinearDirection = App.Vector(0.0, 2.0, 0.0)
            pattern.Spacing = 25.0
            transforms = pattern_transforms(pattern)
            self.assertEqual(len(transforms), 2)
            self.assertTrue(
                transforms[0].Base.isEqual(App.Vector(0.0, 25.0, 0.0), 1e-9)
            )
            self.assertTrue(
                transforms[1].Base.isEqual(App.Vector(0.0, 50.0, 0.0), 1e-9)
            )

            coordinate_system = doc.addObject(
                "Part::LocalCoordinateSystem", "LinearCoordinates"
            )
            pattern.LinearDirectionReference = (
                coordinate_system.OriginFeatures[0],
                [""],
            )
            pattern.ReverseLinearDirection = True
            transforms = pattern_transforms(pattern)
            self.assertTrue(
                linear_direction_definition(pattern).isEqual(
                    App.Vector(-1.0, 0.0, 0.0), 1e-9
                )
            )
            self.assertTrue(
                transforms[0].Base.isEqual(App.Vector(-25.0, 0.0, 0.0), 1e-9)
            )

            pattern.PatternType = "Polar"
            pattern.Occurrences = 4
            pattern.PolarCenter = App.Vector()
            pattern.PolarAxis = App.Vector(0.0, 0.0, 1.0)
            pattern.TotalAngle = 360.0
            pattern.Closed = True
            transforms = pattern_transforms(pattern)
            rotated = transforms[0].multVec(App.Vector(10.0, 0.0, 0.0))
            self.assertTrue(rotated.isEqual(App.Vector(0.0, 10.0, 0.0), 1e-9))
        finally:
            App.closeDocument(doc.Name)

    def test_polar_pattern_creates_part_instances_and_fixed_part_holes(self):
        doc = App.newDocument("ConnectedPatternGeometry")
        try:
            _source, _fixed, connection, pattern, instance_link, cut = (
                _configured_polar_pattern(doc)
            )
            pattern.Occurrences = 4
            doc.recompute()

            self.assertEqual(connection.BoltCount, 4)
            self.assertEqual(pattern.AdditionalOccurrences, 3)
            self.assertEqual(pattern.ConnectionInstanceCount, 4)
            self.assertEqual(instance_link.ElementCount, 3)
            self.assertEqual(len(instance_link.ElementList), 3)
            self.assertTrue(cut.Shape.isValid())
            self.assertEqual(len(cut.Shape.Solids), 1)

            diameter = 0.344 * 25.4
            expected_removed = 4.0 * math.pi * (diameter * 0.5) ** 2 * 2.0
            self.assertAlmostEqual(cut.RemovedVolume.Value, expected_removed, places=4)
            self.assertEqual(cut.AggregateContributorCount, 1)
            self.assertEqual(str(cut.OccurrenceScope), "All Occurrences")

            centers = [
                element.Shape.BoundBox.Center for element in instance_link.ElementList
            ]
            expected_centers = [
                App.Vector(0.0, 10.0, 1.0),
                App.Vector(-10.0, 0.0, 1.0),
                App.Vector(0.0, -10.0, 1.0),
            ]
            for expected in expected_centers:
                self.assertTrue(
                    any(center.isEqual(expected, 1e-7) for center in centers)
                )
        finally:
            App.closeDocument(doc.Name)

    def test_configuration_expression_drives_parts_and_holes_together(self):
        doc = App.newDocument("ConnectedPatternConfiguration")
        try:
            _source, _fixed, _connection, pattern, instance_link, cut = (
                _configured_polar_pattern(doc)
            )
            configuration = doc.addObject("App::FeaturePython", "Configuration")
            configuration.addProperty("App::PropertyInteger", "PatternCount")
            configuration.PatternCount = 4
            pattern.setExpression("Occurrences", "Configuration.PatternCount")
            doc.recompute()

            four_occurrence_volume = cut.RemovedVolume.Value
            self.assertEqual(int(pattern.Occurrences), 4)
            self.assertEqual(instance_link.ElementCount, 3)
            self.assertEqual(pattern.ConnectionInstanceCount, 4)

            configuration.PatternCount = 2
            doc.recompute()
            self.assertEqual(int(pattern.Occurrences), 2)
            self.assertEqual(instance_link.ElementCount, 1)
            self.assertEqual(pattern.AdditionalOccurrences, 1)
            self.assertEqual(pattern.ConnectionInstanceCount, 2)
            self.assertGreater(cut.RemovedVolume.Value, 0.0)
            self.assertLess(cut.RemovedVolume.Value, four_occurrence_volume)

            configuration.PatternCount = 1
            doc.recompute()
            self.assertEqual(instance_link.ElementCount, 0)
            self.assertFalse(instance_link.Visibility)
            self.assertEqual(pattern.AdditionalOccurrences, 0)
            self.assertEqual(pattern.ConnectionInstanceCount, 1)
            diameter = 0.344 * 25.4
            seed_removed = math.pi * (diameter * 0.5) ** 2 * 2.0
            self.assertAlmostEqual(cut.RemovedVolume.Value, seed_removed, places=4)
            self.assertEqual(cut.AggregateContributorCount, 1)
        finally:
            App.closeDocument(doc.Name)

    def test_connected_pattern_survives_save_and_reopen(self):
        doc = App.newDocument("ConnectedPatternPersistence")
        path = os.path.join(tempfile.gettempdir(), "ConnectedPatternPersistence.FCStd")
        try:
            _source, _fixed, _connection, pattern, instance_link, cut = (
                _configured_polar_pattern(doc)
            )
            pattern.Occurrences = 5
            doc.recompute()
            names = (pattern.Name, instance_link.Name, cut.Name)
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            reopened.recompute()
            restored_pattern = reopened.getObject(names[0])
            restored_link = reopened.getObject(names[1])
            restored_cut = reopened.getObject(names[2])
            self.assertEqual(int(restored_pattern.Occurrences), 5)
            self.assertEqual(restored_pattern.AdditionalOccurrences, 4)
            self.assertEqual(restored_link.ElementCount, 4)
            self.assertEqual(restored_pattern.ConnectionInstanceCount, 5)
            self.assertTrue(restored_cut.Shape.isValid())
            self.assertGreater(restored_cut.RemovedVolume.Value, 0.0)
            self.assertEqual(restored_cut.AggregateContributorCount, 1)
        finally:
            if App.ActiveDocument is not None:
                App.closeDocument(App.ActiveDocument.Name)
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
