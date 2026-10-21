"""Retired with member-ban and domain-archive removal."""
import unittest


class ArchiveTests(unittest.TestCase):
    @unittest.skip("domain archive feature removed")
    def test_retired(self):
        pass
