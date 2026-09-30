"""Trimarr: trims SponsorBlock-labelled sponsor segments out of videos already in the Plex "YouTube" library.

A separate tool from Tubarr (the downloader). It never touches Tubarr's code, container, database or .staging,
and it replaces each video in place (same path and name), so the Plex item and its watch state stay.
"""
__version__ = "0.1.1"
