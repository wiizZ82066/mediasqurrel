"""Synthetic source pairs and isolated archive tests; no platform/private data."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image, ImageDraw

import weibo_downloader as downloader
from app import archive, media_library, weibo_images
from app.progress import DownloadProgress, PREFIX


def jpeg(image):
    stream = io.BytesIO()
    image.save(stream, 'JPEG', quality=96)
    return stream.getvalue()


def pair(position='center', marked=True, bright_gap=False):
    y, x = np.mgrid[:1080, :1440]
    texture = (105 + 22*np.sin(x/13) + 17*np.cos(y/19) + x/65).clip(0,255).astype(np.uint8)
    base = Image.fromarray(np.stack((texture, np.roll(texture,5,0),np.roll(texture,7,1)),axis=2))
    if bright_gap:
        ImageDraw.Draw(base).rectangle((630,990,740,1079),fill='white')
    reference = jpeg(base.resize((690,518),Image.Resampling.LANCZOS))
    if marked:
        stamp = Image.new('L',(100,16))
        ImageDraw.Draw(stamp).text((2,1),'@Example12345',fill=235)
        stamp = stamp.resize((290,47),Image.Resampling.LANCZOS)
        left = {'left':20,'center':575,'right':1130}[position]
        base.paste((255,255,255),(left,1010,left+290,1057),stamp)
    return jpeg(base), reference


class WeiboImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ms-weibo-images-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        self.stream = contextlib.redirect_stdout(self.output)
        self.stream.__enter__()
        self.addCleanup(self.stream.__exit__,None,None,None)
        self.source, self.reference = pair()

    def store_pair(self, source=None, reference=None):
        high, low = self.root/'source.jpg', self.root/'reference.jpg'
        high.write_bytes(source or self.source)
        low.write_bytes(reference or self.reference)
        return high, low

    def transfer(self, payload):
        def fake(_url, destination, **kwargs):
            Path(destination).write_bytes(payload)
            progress = kwargs.get('progress')
            if progress:
                progress.start_file(len(payload))
                progress.advance(len(payload))
                progress.finish_file()
            return True
        return fake

    def test_lossless_output_changes_only_a_small_region_for_each_platform_position(self):
        for position in ('left','center','right'):
            with self.subTest(position=position):
                high, low = self.store_pair(*pair(position))
                before = np.array(Image.open(high).convert('RGB'))
                destination = self.root/'patched.webp'
                result = weibo_images.patch_image(high,low,destination)
                with Image.open(destination) as image:
                    self.assertEqual(image.format,'WEBP')
                    self.assertEqual(image.size,(1440,1080))
                    after = np.array(image.convert('RGB'))
                changed = np.any(before != after,axis=2)
                self.assertTrue(changed.any())
                x0,y0,x1,y1 = result['patch_box']
                changed[y0:y1,x0:x1] = False
                self.assertFalse(changed.any())
                self.assertLess(result['changed_pixels_percent'],3)

    def test_mismatched_reference_and_unmarked_original_do_not_publish(self):
        for source, reference in ((self.source,jpeg(Image.new('RGB',(690,518),'red'))),pair(marked=False)):
            high,low = self.store_pair(source,reference)
            destination = self.root/'unchanged.webp'
            destination.write_bytes(b'previous')
            with self.assertRaises(weibo_images.PatchSkipped):
                weibo_images.patch_image(high,low,destination)
            self.assertEqual(destination.read_bytes(),b'previous')

    def test_watermark_interrupted_by_a_bright_background_stays_one_local_patch(self):
        high,low = self.store_pair(*pair(bright_gap=True))
        result = weibo_images.patch_image(high,low,self.root/'bright.webp')
        self.assertLess(result['changed_pixels_percent'],3)
        x0,_,x1,_ = result['patch_box']
        self.assertLess(x0,630)
        self.assertGreater(x1,740)

    def test_failed_output_encoding_preserves_previous_output_and_releases_temporary(self):
        high,low = self.store_pair()
        destination = self.root/'existing.webp'
        destination.write_bytes(b'previous')
        with patch.object(Image.Image,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                weibo_images.patch_image(high,low,destination)
        self.assertEqual(destination.read_bytes(),b'previous')
        self.assertEqual(list(self.root.glob('*.part')),[])

    def test_reference_rewrite_is_limited_to_same_sina_file(self):
        self.assertEqual(weibo_images.reference_url('https://wx1.sinaimg.cn/large/abcdef.jpg?x=1'),
                         'https://wx1.sinaimg.cn/oslarge/abcdef.jpg?x=1')
        for source in ('https://sinaimg.cn.evil.invalid/large/abc.jpg','http://127.0.0.1/large/abc.jpg',
                       'https://user:pass@wx1.sinaimg.cn/large/abc.jpg',
                       'https://wx1.sinaimg.cn:9000/large/abc.jpg',
                       'https://wx1.sinaimg.cn/large/../abc.jpg','file:///large/abc.jpg'):
            self.assertIsNone(weibo_images.reference_url(source))

    def test_archive_preserves_source_and_counts_one_photo_not_the_backup(self):
        data = {'username':'fixture','publish_time':'26-01-01','text':'fixture',
                'url':'https://weibo.com/123/Ab123','mblogid':'Ab123',
                'normal_imgs':['https://wx1.sinaimg.cn/large/abcdef.jpg'],'live_videos':[], 'media_order':[('photo',1)]}
        with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)) as source, \
                patch.object(downloader,'_download_file_once',side_effect=self.transfer(self.reference)) as reference:
            folder = Path(downloader.save_content(data,str(self.root)))
            again = downloader.save_content(data,str(self.root))
        self.assertEqual(again,str(folder))
        source.assert_called_once()
        reference.assert_called_once()
        self.assertEqual(archive.entry_status(folder,verify_files=True),'complete')
        self.assertEqual(archive.read_manifest(folder)['media_order'], ['photo/img01.webp'])
        self.assertEqual((folder/'.originals/img01.jpg').read_bytes(),self.source)
        metadata = archive.read_manifest(folder)['photo_processing'][0]
        self.assertEqual(metadata['original_sha256'],hashlib.sha256(self.source).hexdigest())
        self.assertEqual(metadata['status'],'patched')
        with patch.object(media_library,'_face_center_of',return_value=None):
            entry = media_library._scan_entry(str(folder))
        self.assertEqual(entry['photos'],['photo/img01.webp'])
        self.assertEqual(len(entry['gallery']),1)
        self.assertGreaterEqual(entry['size'],len(self.source)+(folder/'photo/img01.webp').stat().st_size)
        self.assertEqual(list(folder.glob('.weibo-image-*')),[])

    def test_unavailable_or_invalid_reference_preserves_original_bytes_and_one_progress_completion(self):
        for failure in ('offline','invalid','mismatch'):
            with self.subTest(failure=failure):
                progress = MagicMock()
                candidate = (lambda *_args,**_kw:False) if failure=='offline' else self.transfer(
                    b'<html>error</html>' if failure=='invalid' else jpeg(Image.new('RGB',(690,518),'red')))
                with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)), \
                        patch.object(downloader,'_download_file_once',side_effect=candidate):
                    record = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1,progress)
                self.assertEqual(record['status'],'original')
                self.assertEqual((self.root/record['file']).read_bytes(),self.source)
                progress.finish_file.assert_called_once()
                self.assertFalse((self.root/'.originals').exists())
                self.assertEqual(list(self.root.glob('.weibo-image-*')),[])

    def test_corrupt_original_never_overwrites_existing_photo_or_counts_success(self):
        photo = self.root/'photo/img01.jpg'
        photo.parent.mkdir()
        photo.write_bytes(b'existing')
        progress = MagicMock()
        with patch.object(downloader,'download_file',side_effect=self.transfer(b'<html>denied</html>')), \
                patch.object(downloader,'_download_file_once') as candidate:
            result = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1,progress)
        self.assertIsNone(result)
        self.assertEqual(photo.read_bytes(),b'existing')
        progress.finish_file.assert_not_called()
        candidate.assert_not_called()

    def test_patched_photo_is_counted_only_after_publication(self):
        with patch.dict(os.environ, {'MS_PROGRESS':'1'}):
            progress = DownloadProgress(1)
        publish = downloader._publish_image

        def observe(source, destination):
            self.assertEqual(progress.completed, 0)
            publish(source, destination)

        with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)), \
                patch.object(downloader,'_download_file_once',side_effect=self.transfer(self.reference)), \
                patch.object(downloader,'_publish_image',side_effect=observe):
            result = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1,progress)
        self.assertEqual(result['status'],'patched')
        events = [json.loads(line[len(PREFIX):]) for line in self.output.getvalue().splitlines()
                  if line.startswith(PREFIX)]
        self.assertTrue(all(event['completed'] == 0 for event in events[:-1]))
        self.assertEqual(events[-1]['completed'],1)
        self.assertEqual(events[-1]['total'],1)
        self.assertTrue((self.root/result['file']).is_file())

    def test_failed_publication_keeps_original_and_does_not_count_success(self):
        progress = MagicMock()
        publish = downloader._publish_image

        def fail_preview(source, destination):
            if destination.parent.name == 'photo':
                raise OSError('disk full')
            publish(source, destination)

        with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)), \
                patch.object(downloader,'_download_file_once',side_effect=self.transfer(self.reference)), \
                patch.object(downloader,'_publish_image',side_effect=fail_preview):
            result = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1,progress)
        self.assertIsNone(result)
        self.assertEqual((self.root/'.originals/img01.jpg').read_bytes(),self.source)
        progress.finish_file.assert_not_called()
        self.assertEqual(list(self.root.glob('.weibo-image-*')),[])

    def test_partial_retry_to_original_does_not_leave_duplicate_gallery_files(self):
        with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)), \
                patch.object(downloader,'_download_file_once',side_effect=self.transfer(self.reference)):
            first = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1)
        self.assertEqual(first['status'],'patched')
        with patch.object(downloader,'download_file',side_effect=self.transfer(self.source)), \
                patch.object(downloader,'_download_file_once',return_value=False):
            second = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.jpg',self.root,1)
        self.assertEqual(second['file'],'photo/img01.jpg')
        self.assertEqual([p.name for p in (self.root/'photo').iterdir()],['img01.jpg'])
        self.assertEqual((self.root/'.originals/img01.jpg').read_bytes(),self.source)

    def test_reference_transfer_is_bounded_and_does_not_log_signed_urls(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {}
        response.iter_content.return_value = [b'first',b'second']
        destination = self.root/'reference'
        with patch.object(downloader.requests,'get',return_value=response):
            self.assertFalse(downloader._download_file_once('https://wx1.sinaimg.cn/oslarge/a.jpg?private=value',
                                                           str(destination),max_bytes=6,timeout=20,quiet=True))
        self.assertFalse(destination.exists())
        self.assertNotIn('private=value',self.output.getvalue())
        self.assertEqual(list(self.root.glob('*.part')),[])

    def test_animated_source_is_preserved_without_requesting_reference(self):
        stream = io.BytesIO()
        Image.new('RGB',(300,300),'red').save(stream,'GIF',save_all=True,
                                             append_images=[Image.new('RGB',(300,300),'blue')],duration=100,loop=0)
        original = stream.getvalue()
        with patch.object(downloader,'download_file',side_effect=self.transfer(original)), \
                patch.object(downloader,'_download_file_once') as reference:
            result = downloader.download_photo('https://wx1.sinaimg.cn/large/abcdef.gif',self.root,1)
        self.assertEqual(result['file'],'photo/img01.gif')
        self.assertEqual((self.root/result['file']).read_bytes(),original)
        reference.assert_not_called()


if __name__ == '__main__':
    unittest.main()
