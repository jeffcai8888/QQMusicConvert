"""离线回归测试: 普通 OGG/FLAC 与加密 MGG 转 MP3, 验证音频、标签及源文件保留。"""

import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import mflac2mp3 as M


FFMPEG = M.find_ffmpeg()
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg 和 ffprobe")
class MggConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="mgg-tests-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.sources = {}
        for name, codec, ext, tags in (
            ("tagged", "libvorbis", "ogg", {
                "title": "音轨曲名", "artist": "音轨歌手", "album": "测试专辑",
                "description": "x" * 70000,  # 强制 comment packet 跨 Ogg 页。
            }),
            ("partial", "libvorbis", "ogg", {"artist": "音轨歌手"}),
            ("opus", "libopus", "ogg", {"title": "Opus 曲名", "album": "Opus 专辑"}),
            ("flac", "flac", "flac", {"title": "FLAC 曲名", "album": "FLAC 专辑"}),
        ):
            metadata = cls.root / (name + ".txt")
            metadata.write_text(";FFMETADATA1\n" + "\n".join(
                f"{key}={value}" for key, value in tags.items()), encoding="utf-8")
            path = cls.root / f"{name}.{ext}"
            subprocess.run([
                FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.4", "-f", "ffmetadata", "-i",
                str(metadata), "-map_metadata", "1", "-c:a", codec, str(path),
            ], check=True, capture_output=True)
            cls.sources[name] = path.read_bytes()

    def encrypt(self, name, source="tagged", trailer="ekey", key_size=128):
        audio = bytearray(self.sources[source])
        if trailer == "static":
            M.StaticCipher().decrypt(audio)
            footer = b""
        else:
            key = bytes((i * 7 % 255) + 1 for i in range(key_size))
            ekey = base64.b64encode(key[:8] + M.tc_tea_encrypt(
                key[8:], M.derive_tea_key(key[:8])))
            M.make_cipher(ekey.decode()).decrypt(audio)
            if trailer == "qtag":
                raw = ekey + b",12345,2"
                footer = raw + struct.pack(">I", len(raw)) + b"QTag"
            else:
                footer = ekey + struct.pack("<I", len(ekey))
        path = self.root / name
        path.write_bytes(audio + footer)
        return path

    def convert(self, path, **options):
        ctx = M.ConvertContext(M.ConvertOptions(
            output_dir=self.root / "output", no_cache=True, ffmpeg=FFMPEG, **options))
        ok, message = M.convert_one(path, ctx)
        self.assertTrue(ok, message)
        mp3 = ctx.out_dir / (path.stem + ".mp3")
        info = json.loads(subprocess.check_output([
            FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(mp3),
        ]))
        self.assertEqual(info["streams"][0]["codec_name"], "mp3")
        self.assertAlmostEqual(float(info["format"]["duration"]), 0.4, delta=0.1)
        subprocess.run([
            FFMPEG, "-v", "error", "-xerror", "-i", str(mp3), "-f", "null", "-",
        ], check=True, capture_output=True)
        self.assertFalse(list(ctx.out_dir.glob(".*.tmp.*")))
        return info["format"]["tags"]

    def test_ogg_comments_across_pages(self):
        audio = self.sources["tagged"]
        tags = M.ogg_read_tags(memoryview(audio))
        self.assertEqual(tags["TITLE"], "音轨曲名")
        self.assertEqual(tags["ALBUM"], "测试专辑")
        self.assertEqual(len(tags["DESCRIPTION"]), 70000)
        # 第一页后的 comment packet 被截断时不能读出伪造标签。
        self.assertEqual(M.ogg_read_tags(audio[:200]), {})
        self.assertEqual(M.ogg_read_tags(b"OggS"), {})
        self.assertEqual(M.ogg_read_tags(self.sources["flac"]), {})

    def test_mgg_trailers_and_ciphers_preserve_tags(self):
        for trailer, size in (("static", 0), ("ekey", 128), ("ekey", 400),
                              ("qtag", 128), ("qtag", 400)):
            with self.subTest(trailer=trailer, key_size=size):
                path = self.encrypt(f"文件名歌手 - {trailer}{size}.mgg",
                                    trailer=trailer, key_size=size)
                original = path.read_bytes()
                tags = self.convert(path)
                self.assertEqual(tags["title"], "音轨曲名")
                self.assertEqual(tags["artist"], "音轨歌手")
                self.assertEqual(tags["album"], "测试专辑")
                self.assertEqual(path.read_bytes(), original)

    def test_only_missing_tags_use_filename(self):
        path = self.encrypt("文件名歌手 - 缺失曲名.MGG", source="partial")
        tags = self.convert(path, keep_flac=True)
        self.assertEqual(tags["artist"], "音轨歌手")
        self.assertEqual(tags["title"], "缺失曲名")
        self.assertEqual((self.root / "output" / (path.stem + ".ogg")).read_bytes(),
                         self.sources["partial"])
        self.assertIn(path, M.collect_inputs([str(self.root)]))

    def test_ogg_opus_tags(self):
        path = self.encrypt("文件名歌手 - Opus.mgg", source="opus")
        tags = self.convert(path)
        self.assertEqual(tags["title"], "Opus 曲名")
        self.assertEqual(tags["album"], "Opus 专辑")
        self.assertEqual(tags["artist"], "文件名歌手")

    def test_flac_tags_still_preserved(self):
        path = self.encrypt("文件名歌手 - FLAC.mflac", source="flac")
        tags = self.convert(path)
        self.assertEqual(tags["title"], "FLAC 曲名")
        self.assertEqual(tags["album"], "FLAC 专辑")
        self.assertEqual(tags["artist"], "文件名歌手")

    def test_decrypt_only_mgg_keeps_ogg(self):
        path = self.encrypt("只解密.mgg", source="partial")
        output = self.root / "decrypted"
        ctx = M.ConvertContext(M.ConvertOptions(
            output_dir=output, flac_only=True, no_cache=True))
        ok, message = M.convert_one(path, ctx)
        self.assertTrue(ok, message)
        self.assertEqual((output / "只解密.ogg").read_bytes(), self.sources["partial"])
        self.assertFalse((output / "只解密.mp3").exists())

    def test_plain_ogg_and_flac_convert_without_decryption(self):
        for source, ext, title, album in (
            ("tagged", "ogg", "音轨曲名", "测试专辑"),
            ("flac", "flac", "FLAC 曲名", "FLAC 专辑"),
        ):
            for keep in (False, True):
                with self.subTest(ext=ext, keep=keep):
                    path = self.root / f"文件名歌手 - 普通{source}{keep}.{ext}"
                    path.write_bytes(self.sources[source])
                    modified = path.stat().st_mtime_ns
                    with patch.object(M, "detect_trailer", side_effect=AssertionError("不应解密")):
                        tags = self.convert(path, keep_flac=keep)
                    self.assertEqual(tags["title"], title)
                    self.assertEqual(tags["album"], album)
                    self.assertEqual(path.read_bytes(), self.sources[source])
                    self.assertEqual(path.stat().st_mtime_ns, modified)
                    self.assertFalse((self.root / "output" / path.name).exists())

    def test_plain_audio_same_directory_preserves_source(self):
        for source, ext in (("partial", "OGG"), ("flac", "FLAC")):
            path = self.root / f"原目录{source}.{ext}"
            path.write_bytes(self.sources[source])
            modified = path.stat().st_mtime_ns
            ctx = M.ConvertContext(M.ConvertOptions(keep_flac=True, overwrite=True))
            ok, message = M.convert_one(path, ctx)
            self.assertTrue(ok, message)
            self.assertTrue(path.with_suffix(".mp3").is_file())
            self.assertEqual(path.read_bytes(), self.sources[source])
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_decrypt_only_skips_plain_audio(self):
        path = self.root / "普通只解密.ogg"
        path.write_bytes(self.sources["partial"])
        output = self.root / "plain-decrypt-only"
        ctx = M.ConvertContext(M.ConvertOptions(output_dir=output, flac_only=True, overwrite=True))
        ok, message = M.convert_one(path, ctx)
        self.assertTrue(ok, message)
        self.assertIn("无需解密", message)
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual(path.read_bytes(), self.sources["partial"])

    def test_plain_audio_failures_preserve_source(self):
        path = self.root / "转换失败.ogg"
        path.write_bytes(self.sources["partial"])
        ctx = M.ConvertContext(M.ConvertOptions())
        with patch.object(M, "run_ffmpeg", side_effect=RuntimeError("转码失败")):
            with self.assertRaisesRegex(RuntimeError, "转码失败"):
                M.convert_one(path, ctx)
        self.assertEqual(path.read_bytes(), self.sources["partial"])
        ctx.ffmpeg = None
        ok, message = M.convert_one(path, ctx)
        self.assertFalse(ok)
        self.assertIn("未找到 ffmpeg", message)
        self.assertEqual(path.read_bytes(), self.sources["partial"])

    def test_invalid_plain_audio_is_rejected(self):
        for ext in ("ogg", "flac"):
            path = self.root / f"无效.{ext}"
            path.write_bytes(b"invalid audio" * 8)
            with patch.object(M, "detect_trailer", side_effect=AssertionError("不应解密")):
                ok, message = M.convert_one(path, M.ConvertContext(M.ConvertOptions()))
            self.assertFalse(ok)
            self.assertIn("不是有效的 OGG/FLAC", message)

    def test_collect_mixed_audio_directory(self):
        folder = self.root / "mixed-input"
        nested = folder / "子目录.ogg"
        nested.mkdir(parents=True)
        expected = [folder / "原音.OGG", nested / "无损.FLAC", folder / "加密.mgg"]
        for path in expected + [folder / "已转换.mp3"]:
            path.touch()
        self.assertEqual(set(M.collect_inputs([str(folder), str(expected[0])])), set(expected))


if __name__ == "__main__":
    unittest.main()
