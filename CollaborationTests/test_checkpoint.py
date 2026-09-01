"""Revision-zero checkpoint persistence tests."""

from __future__ import annotations

import unittest

import FreeCAD as App

from freecad_collaboration import bootstrap_document, document_state
from freecad_collaboration.checkpoint import (
    is_native_document_archive,
    native_checkpoint_bytes,
    open_checkpoint,
)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.documents = []

    def tearDown(self):
        for document in reversed(self.documents):
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass

    def new_document(self, name):
        document = App.newDocument(name)
        self.documents.append(document)
        return document

    def test_native_checkpoint_reopens_as_fcstd(self):
        source = self.new_document("NativeCheckpointSource")
        source.addObject("Part::Box", "Box").Length = 42
        bootstrap_document(source)
        source.recompute()

        checkpoint = native_checkpoint_bytes(source)
        self.assertTrue(is_native_document_archive(checkpoint))
        restored = open_checkpoint(checkpoint)
        self.documents.append(restored)

        self.assertEqual(document_state(source), document_state(restored))

    def test_legacy_persistence_checkpoint_remains_supported(self):
        source = self.new_document("LegacyCheckpointSource")
        source.addObject("Part::Box", "Box").Width = 17
        bootstrap_document(source)
        source.recompute()

        checkpoint = bytes(source.dumpContent(9))
        self.assertFalse(is_native_document_archive(checkpoint))
        restored = open_checkpoint(checkpoint, document_name="LegacyCheckpointTarget")
        self.documents.append(restored)

        self.assertEqual(document_state(source), document_state(restored))


if __name__ == "__main__":
    unittest.main()
