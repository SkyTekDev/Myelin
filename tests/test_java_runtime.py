"""Shared JVM setup and platform-safe isolated JAR loading."""

import logging
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import jpype

from myelin import Myelin
from myelin.pricers.url_loader import UrlLoader


class JavaRuntimeChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not jpype.isJVMStarted():
            jpype.startJVM(convertStrings=True)

    def test_local_jar_with_spaces_and_unicode_uses_its_own_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'CMS pricers é'
            directory.mkdir()
            jar = directory / 'pricing data ü.jar'
            resource = 'myelin-runtime-check.txt'
            with ZipFile(jar, 'w') as archive:
                archive.writestr(resource, 'isolated JAR resource')
            loader = UrlLoader()
            loader.load_paths([jar])
            stream = None
            try:
                stream = loader.class_loader.getResourceAsStream(resource)
                self.assertIsNotNone(stream)
                self.assertEqual(bytes(stream.readAllBytes()), b'isolated JAR resource')
            finally:
                if stream is not None:
                    stream.close()
                loader.class_loader.close()

    def test_myelin_adds_its_classpath_to_an_already_started_jvm(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            jars = directory / 'empty jars'
            jars.mkdir()
            extra = directory / 'extra runtime é'
            extra.mkdir()
            resource = f'myelin-runtime-{uuid4().hex}.txt'
            (extra / resource).write_text('additional runtime dependency', encoding='utf-8')
            myelin = Myelin.__new__(Myelin)
            myelin.jar_path = str(jars)
            myelin.extra_classpaths = [str(extra)]
            myelin.logger = logging.getLogger(__name__)
            myelin._setup_jvm()
            loader = jpype.JClass('org.jpype.JPypeContext').getInstance().getClassLoader()
            stream = loader.getResourceAsStream(resource)
            self.assertIsNotNone(stream)
            try:
                self.assertEqual(bytes(stream.readAllBytes()), b'additional runtime dependency')
            finally:
                stream.close()
