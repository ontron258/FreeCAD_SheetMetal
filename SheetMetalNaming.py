########################################################################
#
#  SheetMetalNaming.py
#
#  Copyright 2026 SheetMetal Workbench contributors
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
########################################################################

"""Compatibility entry points; drawing naming now belongs to Release Manager.

SheetMetal modeling is usable without that optional addon. Existing metadata is
retained; installing Release Manager resumes name maintenance on document load.
"""
try:
    from freecad_release_manager.naming import (
        DRAWING_NAME_SEPARATOR, drawingGroupPath, computedDrawingName,
        updateDrawingName, addDrawingIdentityProperties, upgradeDocumentDrawingNames,
    )
except ModuleNotFoundError as error:
    if error.name not in ("freecad_release_manager", "freecad_release_manager.naming"):
        raise
    DRAWING_NAME_SEPARATOR = " – "

    def drawingGroupPath(part):
        return []

    def computedDrawingName(part):
        return getattr(part, "DrawingName", part.Label)

    def updateDrawingName(part):
        pass

    def addDrawingIdentityProperties(part):
        pass

    def upgradeDocumentDrawingNames(doc):
        pass
