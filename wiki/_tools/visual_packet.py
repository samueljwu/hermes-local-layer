"""Wiki-local pixel packets. Preparation is NEVER transport or reading approval.

Only pinned page rasters are used; text/OCR cannot supply image content. Detail
payloads are exact pixel crops, optionally composed with repeated header/footnote
strips. Full-page previews are context only. No network/model/core settings.
"""
from __future__ import annotations

import io
from pathlib import Path
import shutil

import visual_evidence as ve

PACKET_VERSION = 2
MAX_EDGE = 1568
MAX_ITEMS = 4096
MAX_PACKET_BYTES = 128 * 1024 * 1024


def _box(value, size):
    ve.need(isinstance(value, list) and len(value) == 4 and all(type(x) is int for x in value), 'packet bbox must use integer raster pixels')
    return ve._bbox(value, [0, 0, *size])


def _packet_read(root, name, limit):
    ve.need(isinstance(name, str) and Path(name).name == name and name not in ('.', '..'), 'unsafe packet filename')
    asset = root / name
    ve.need(not asset.is_symlink() and asset.is_file(), 'missing/unsafe packet asset')
    ve.need(asset.stat().st_size <= limit, 'packet asset byte budget exceeded')
    with asset.open('rb') as stream:
        data = stream.read(limit + 1)
    ve.need(len(data) <= limit, 'packet asset byte budget exceeded')
    return data


def _starts(lo, hi, length, overlap):
    ve.need(0 < overlap < length, 'overlap must be positive and smaller than tile')
    cursor = lo
    while True:
        yield cursor, min(cursor + length, hi)
        if cursor + length >= hi:
            break
        cursor += length - overlap


def _detail_plan(manifest, regions, max_edge, overlap):
    """Bounded deterministic region/strip/tile plan in stored source pixels."""
    ve.need(isinstance(regions, list) and 0 < len(regions) <= MAX_ITEMS, 'explicit detail regions required')
    count = manifest['page_count']
    planned = 0
    for region in regions:
        ve._keys(region, {'page', 'bbox', 'header', 'footnotes', 'min_label_pixels', 'context_basis'}, 'packet region')
        number = ve._integer(region['page'], 'region page', 1, count)
        page = manifest['pages'][number - 1]
        size = [page['width'], page['height']]
        ve.need(manifest['kind'] != 'pdf' or page['dpi'] >= 300, 'packet detail requires a stored >=300 DPI page; prepare a separate scratch render')
        ve.need(ve._number(region['min_label_pixels']) and region['min_label_pixels'] >= 12,
                'source labels below 12 pixels require higher-resolution evidence, not upsampling')
        ve.need(isinstance(region['context_basis'], str) and region['context_basis'].strip(), 'region needs explicit caption/legend/footnote context basis')
        box = _box(region['bbox'], size)
        strips = []
        for role in ('header', 'footnotes'):
            if region[role] is not None:
                bounds = _box(region[role], size)
                ve.need(bounds[2] - bounds[0] <= max_edge, 'repeated header/footnotes exceed readable width; supply complete column-local context')
                strips.append((role, bounds))
        height = max_edge - sum(b[3] - b[1] + 8 for _, b in strips)
        ve.need(height > overlap, 'header/footnotes leave no readable overlapping body tile')
        for x0, x1 in _starts(box[0], box[2], max_edge, overlap):
            for y0, y1 in _starts(box[1], box[3], height, overlap):
                planned += 1
                ve.need(planned + count <= MAX_ITEMS, 'packet item budget exceeded')
                components = ([s for s in strips if s[0] == 'header'] + [('body', [x0, y0, x1, y1])] +
                              [s for s in strips if s[0] == 'footnotes'])
                width = max(b[2] - b[0] for _, b in components)
                h = sum(b[3] - b[1] for _, b in components) + 8 * (len(components) - 1)
                mappings, y = [], 0
                for role, bounds in components:
                    w, ch = bounds[2] - bounds[0], bounds[3] - bounds[1]
                    mappings.append({'source': page['render'], 'source_dimensions': size,
                        'source_bbox': bounds, 'payload_bbox': [0, y, w, y + ch],
                        'transformation': 'exact-pixel-crop', 'component': role, 'scale': [1, 1]})
                    y += ch + 8
                yield number, [width, h], mappings, region['min_label_pixels'], region['context_basis']


def _source_pages(manifest):
    return [{'page': p['page'], 'render': p['render'], 'stored_dimensions': [p['width'], p['height']],
             'stored_dpi': p['dpi'], 'rotation': p['rotation'], 'kind': manifest['kind']} for p in manifest['pages']]


def prepare_packet(manifest_path, output, regions, *, max_edge=MAX_EDGE, overlap=64,
                   max_image_bytes=2 * 1024 * 1024, max_batch_bytes=8 * 1024 * 1024, max_images=8):
    """All-page context plus explicit risky regions; existing evidence is unchanged.

    Regions: page, bbox (stored raster pixels), header/footnotes (bbox or null),
    min_label_pixels (conservative source glyph-height estimate, NOT an observed
    readability attestation), context_basis (what caption/legend/units it retains).
    Packet-local assets are private preparations, not admitted raw attachments.
    """
    root, rel = ve._manifest_location(manifest_path)
    manifest_bytes = ve._read(root, rel, ve.MAX_MANIFEST_BYTES)
    manifest = ve._json(manifest_bytes)
    ve.validate(manifest, lambda p: ve._read(root, p), require_review=False)
    ve._integer(max_edge, 'packet long-edge limit', 64, MAX_EDGE)
    ve._integer(overlap, 'overlap', 1, max_edge - 1)
    ve._integer(max_image_bytes, 'image bytes', 1, MAX_PACKET_BYTES)
    ve._integer(max_batch_bytes, 'batch bytes', max_image_bytes, MAX_PACKET_BYTES)
    ve._integer(max_images, 'images per batch', 1, MAX_ITEMS)
    ve.need(isinstance(regions, list) and regions and len(regions) <= MAX_ITEMS, 'explicit detail regions required')
    destination = Path(output).absolute()
    ve.need('..' not in destination.parts and not destination.exists(), 'packet output must be new')
    ve._root(destination.parent)
    # Preparation must not touch an admitted tree or live evidence, even if a
    # caller chooses an innocuous-looking nested output directory.
    for ancestor in [destination.parent, *destination.parents]:
        ve.need(not (ancestor / '_meta/ingestion/baseline.json').exists(), 'packet output inside canonical wiki refused')
    _, Image, _ = ve._deps()
    items, total = [], 0
    destination.mkdir(mode=0o700)
    try:
        def emit(image, role, page, mappings, label_pixels=None, basis=None):
            nonlocal total
            ve.need(len(items) < MAX_ITEMS, 'packet item budget exceeded')
            ve.need(max(image.size) <= max_edge, 'packet payload exceeds long-edge budget')
            stream = io.BytesIO()
            image.save(stream, format='PNG')
            data = stream.getvalue()
            ve.need(len(data) <= max_image_bytes, 'packet image exceeds embed byte budget; choose smaller tiles')
            total += len(data)
            ve.need(total <= MAX_PACKET_BYTES, 'packet aggregate byte budget exceeded')
            ident = f'item-{len(items) + 1:06d}'
            path = destination / f'{ident}.png'
            path.write_bytes(data)
            items.append({'id': ident, 'path': path.name, 'sha256': ve.sha(data),
                          'role': role, 'page': page, 'intended_dimensions': list(image.size),
                          'encoded_bytes': len(data), 'mappings': mappings,
                          'min_label_pixels': label_pixels, 'context_basis': basis})

        for page in manifest['pages']:
            with Image.open(io.BytesIO(ve._read(root, page['render']['path']))) as stored:
                size = stored.size
                preview = stored.convert('RGB')
                preview.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                try:
                    emit(preview, 'full-page-context-only', page['page'], [{
                        'source': page['render'], 'source_dimensions': list(size),
                        'source_bbox': [0, 0, *size], 'payload_bbox': [0, 0, *preview.size],
                        'transformation': 'context-only-resize', 'scale': [preview.width / size[0], preview.height / size[1]]}])
                finally:
                    preview.close()

        for number, dimensions, mappings, label, basis in _detail_plan(manifest, regions, max_edge, overlap):
            page = manifest['pages'][number - 1]
            with Image.open(io.BytesIO(ve._read(root, page['render']['path']))) as stored:
                with Image.new('RGB', tuple(dimensions), 'white') as payload:
                    for mapping in mappings:
                        with stored.crop(mapping['source_bbox']).convert('RGB') as clipped:
                            payload.paste(clipped, tuple(mapping['payload_bbox'][:2]))
                    emit(payload, 'detail', number, mappings, label, basis)
        ve.need(ve._read(root, rel, ve.MAX_MANIFEST_BYTES) == manifest_bytes, 'manifest drift during packet preparation')
        # Recheck original/native extraction and every asset after preparation.
        ve.validate(manifest, lambda p: ve._read(root, p), require_review=False)
        batches, batch, byte_count = [], [], 0
        for item in items:
            if batch and (len(batch) == max_images or byte_count + item['encoded_bytes'] > max_batch_bytes):
                batches.append(batch)
                batch, byte_count = [], 0
            batch.append(item['id'])
            byte_count += item['encoded_bytes']
        if batch:
            batches.append(batch)
        packet = {'version': PACKET_VERSION, 'status': 'prepared-unobserved',
                  'source_manifest_path': str(Path(manifest_path).absolute()), 'overlap': overlap,
                  'manifest': {'path': rel, 'sha256': ve.sha(manifest_bytes)}, 'original': manifest['original'],
                  'native_extraction_sha256': ve.sha(ve._read(root, str(Path(rel).parent / 'extraction.txt'))),
                  'limits': {'max_edge': max_edge, 'max_image_bytes': max_image_bytes,
                             'max_batch_bytes': max_batch_bytes, 'max_images': max_images},
                  'regions': regions, 'items': items, 'batches': batches,
                  'coordinate_system': 'stored-page-raster-pixels; original mapping is pinned by manifest page, DPI and rotation',
                  'source_pages': _source_pages(manifest),
                  'transport_note': 'No actual transport observed. Adapter must verify post-transform model-input pixels and actual embed budget.'}
        (destination / 'packet.json').write_bytes(ve._serialize(packet))
        validate_packet(destination / 'packet.json')
        return {'packet': str(destination / 'packet.json'), 'sha256': ve.sha((destination / 'packet.json').read_bytes()),
                'items': len(items), 'batches': len(batches), 'status': 'prepared-unobserved'}
    except Exception:
        shutil.rmtree(destination)
        raise


def validate_packet(packet_path, *, manifest_path=None, expected_sha256=None):
    """Source-aware intrinsic fidelity; not source interpretation/authenticity.

    The adapter must supply its independently retained preparation digest and
    manifest location. Packet-local paths/self-hashes are not authentication.
    Without those arguments, this checks consistency against the named source.
    """
    try:
        return _validate_packet(packet_path, manifest_path, expected_sha256)
    except ve.VisualError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ve.VisualError(f'invalid packet/source structure: {exc}') from exc


def _validate_packet(packet_path, manifest_path, expected_sha256):
    path = Path(packet_path).absolute()
    root = ve._root(path.parent)
    ve.need(path.name == 'packet.json' and not path.is_symlink(), 'unsafe packet path')
    packet_bytes = _packet_read(root, path.name, ve.MAX_MANIFEST_BYTES)
    ve.need(expected_sha256 is None or ve.sha(packet_bytes) == expected_sha256, 'trusted preparation packet pin mismatch')
    packet = ve._json(packet_bytes)
    ve._keys(packet, {'version', 'status', 'source_manifest_path', 'overlap', 'manifest', 'original',
        'native_extraction_sha256', 'limits', 'regions', 'items', 'batches', 'coordinate_system',
        'source_pages', 'transport_note'}, 'packet')
    ve.need(type(packet.get('version')) is int and packet['version'] == PACKET_VERSION
            and packet.get('status') == 'prepared-unobserved', 'invalid packet policy/status')
    limits = packet['limits']
    ve._keys(limits, {'max_edge', 'max_images', 'max_image_bytes', 'max_batch_bytes'}, 'packet limits')
    ve._integer(limits['max_edge'], 'packet edge limit', 64, MAX_EDGE)
    ve._integer(limits['max_images'], 'batch image count', 1, MAX_ITEMS)
    ve._integer(limits['max_image_bytes'], 'image bytes', 1, MAX_PACKET_BYTES)
    ve._integer(limits['max_batch_bytes'], 'batch bytes', limits['max_image_bytes'], MAX_PACKET_BYTES)
    ve._integer(packet['overlap'], 'overlap', 1, limits['max_edge'] - 1)
    source_location = packet['source_manifest_path']
    ve.need(isinstance(source_location, str) and Path(source_location).is_absolute(), 'absolute preparation manifest location required')
    ve.need(manifest_path is None or Path(manifest_path).absolute() == Path(source_location), 'trusted preparation manifest location mismatch')
    source_root, rel = ve._manifest_location(source_location)
    manifest_bytes = ve._read(source_root, rel, ve.MAX_MANIFEST_BYTES)
    ve.need(packet['manifest'] == {'path': rel, 'sha256': ve.sha(manifest_bytes)}, 'packet manifest pin mismatch')
    manifest = ve._json(manifest_bytes)
    ve.validate(manifest, lambda p: ve._read(source_root, p), require_review=False)
    ve.need(packet['original'] == manifest['original'], 'packet original pin mismatch')
    ve.need(packet['native_extraction_sha256'] == ve.sha(ve._read(source_root, str(Path(rel).parent / 'extraction.txt'))), 'packet native extraction pin mismatch')
    ve.need(packet['source_pages'] == _source_pages(manifest), 'packet source page mapping differs from pinned manifest')
    ve.need(isinstance(packet['items'], list) and 0 < len(packet['items']) <= MAX_ITEMS, 'empty/oversized packet')
    ids, total = {}, 0
    source_pages = {}
    ve.need(isinstance(packet.get('source_pages'), list) and packet['source_pages'], 'missing source page mapping')
    for page in packet['source_pages']:
        number = ve._integer(page['page'], 'packet source page', 1, ve.MAX_PAGES)
        ve.need(number not in source_pages, 'duplicate packet source page')
        ve._pixel_budget(*page['stored_dimensions'])
        ve._dpi(page['stored_dpi'])
        ve.need(page['kind'] in ('pdf', 'image'), 'invalid packet source kind')
        ve.need(isinstance(page['render'], dict) and set(page['render']) == {'path', 'sha256'}, 'invalid packet source pin')
        ve._rel(page['render']['path'])
        ve.need(isinstance(page['render']['sha256'], str) and len(page['render']['sha256']) == 64,
                'invalid packet source hash')
        source_pages[number] = page
    ve.need(list(source_pages) == list(range(1, len(source_pages) + 1)), 'packet source pages must be complete and ordered')
    contexts = []
    for item in packet['items']:
        ve._keys(item, {'id', 'path', 'sha256', 'role', 'page', 'intended_dimensions', 'encoded_bytes',
                       'mappings', 'min_label_pixels', 'context_basis'}, 'packet item')
        ident = ve._slug(item['id'])
        ve.need(item['path'] == ident + '.png' and ident not in ids, 'unsafe/duplicate packet item')
        asset = root / item['path']
        ve.need(not asset.is_symlink() and asset.is_file(), 'missing/unsafe packet image')
        data = _packet_read(root, item['path'], limits['max_image_bytes'])
        ve.need(ve.sha(data) == item['sha256'] and len(data) == item['encoded_bytes'], 'packet image pin mismatch')
        dimensions = ve._png(data)
        ve.need(list(dimensions) == item['intended_dimensions'] and max(dimensions) <= limits['max_edge'], 'packet image dimension/budget mismatch')
        ve.need(len(data) <= limits['max_image_bytes'], 'packet image byte budget exceeded')
        ve.need(type(item['page']) is int and item['page'] in source_pages, 'packet item source page mismatch')
        source = source_pages[item['page']]
        ve.need(item['role'] in ('full-page-context-only', 'detail'), 'invalid packet item role')
        mappings = item.get('mappings')
        ve.need(isinstance(mappings, list) and mappings, 'packet item requires source/crop mapping')
        if item['role'] == 'full-page-context-only':
            contexts.append(item['page'])
            ve.need(len(mappings) == 1 and item['min_label_pixels'] is None, 'invalid context-only payload')
        else:
            ve.need(source['kind'] != 'pdf' or source['stored_dpi'] >= 300, 'detail source DPI below 300')
            ve.need(ve._number(item['min_label_pixels']) and item['min_label_pixels'] >= 12,
                    'unreadable source labels cannot be upgraded by packet metadata')
            ve.need(isinstance(item['context_basis'], str) and item['context_basis'].strip(), 'detail context basis required')
            ve.need([m.get('component') for m in mappings].count('body') == 1, 'detail must contain one body tile')
        for mapping in mappings:
            ve.need(mapping['source'] == source['render'] and mapping['source_dimensions'] == source['stored_dimensions'], 'packet source/crop pin mismatch')
            sb = _box(mapping['source_bbox'], source['stored_dimensions'])
            pb = _box(mapping['payload_bbox'], dimensions)
            if item['role'] == 'detail':
                ve.need(mapping.get('component') in ('header', 'body', 'footnotes')
                        and mapping['transformation'] == 'exact-pixel-crop' and mapping['scale'] == [1, 1]
                        and sb[2] - sb[0] == pb[2] - pb[0] and sb[3] - sb[1] == pb[3] - pb[1],
                        'detail transformation/scale/crop mismatch')
            else:
                ve.need(mapping['transformation'] == 'context-only-resize'
                        and sb == [0, 0, *source['stored_dimensions']] and pb == [0, 0, *dimensions], 'full-page context mapping incomplete')
                ve.need(mapping['scale'] == [dimensions[0] / source['stored_dimensions'][0], dimensions[1] / source['stored_dimensions'][1]], 'context scale mismatch')
        total += len(data)
        ve.need(total <= MAX_PACKET_BYTES, 'packet byte budget exceeded')
        ids[ident] = item
    ve.need(contexts == list(source_pages), 'missing/duplicate full-page contexts')
    covered = []
    for batch in packet['batches']:
        ve.need(isinstance(batch, list) and 0 < len(batch) <= limits['max_images'] and all(i in ids for i in batch), 'invalid packet batch')
        ve.need(sum(ids[i]['encoded_bytes'] for i in batch) <= limits['max_batch_bytes'], 'packet batch byte budget exceeded')
        covered.extend(batch)
    ve.need(covered == list(ids), 'packet batches must cover each payload exactly once in order')
    _validate_fidelity(packet, root, source_root, manifest)
    ve.need(ve._read(source_root, rel, ve.MAX_MANIFEST_BYTES) == manifest_bytes, 'manifest drift during packet validation')
    ve.validate(manifest, lambda p: ve._read(source_root, p), require_review=False)
    return packet


def _validate_fidelity(packet, root, source_root, manifest):
    """Reconstruct each bounded payload; check geometry AND pixels/padding."""
    _, Image, _ = ve._deps()
    items = iter(packet['items'])
    edge = packet['limits']['max_edge']

    def check(expected, role, page, mappings, label=None, basis=None):
        item = next(items, None)
        ve.need(item is not None, 'missing required context/detail tile coverage')
        assert item is not None
        ve.need(item['role'] == role and item['page'] == page and item['mappings'] == mappings
                and item['min_label_pixels'] == label and item['context_basis'] == basis,
                'packet region tile/strip coverage, order or source-coordinate mismatch')
        with Image.open(io.BytesIO(_packet_read(root, item['path'], packet['limits']['max_image_bytes']))) as actual:
            ve.need(actual.mode == 'RGB' and actual.size == expected.size
                    and actual.tobytes() == expected.tobytes(), 'packet payload pixels differ from pinned source/plan')

    for page in manifest['pages']:
        with Image.open(io.BytesIO(ve._read(source_root, page['render']['path']))) as stored:
            with stored.convert('RGB') as preview:
                preview.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                mappings = [{'source': page['render'], 'source_dimensions': list(stored.size),
                    'source_bbox': [0, 0, *stored.size], 'payload_bbox': [0, 0, *preview.size],
                    'transformation': 'context-only-resize', 'scale': [preview.width / stored.width, preview.height / stored.height]}]
                check(preview, 'full-page-context-only', page['page'], mappings)
    for number, dimensions, mappings, label, basis in _detail_plan(manifest, packet['regions'], edge, packet['overlap']):
        page = manifest['pages'][number - 1]
        with Image.open(io.BytesIO(ve._read(source_root, page['render']['path']))) as stored:
            with Image.new('RGB', tuple(dimensions), 'white') as expected:
                for mapping in mappings:
                    with stored.crop(mapping['source_bbox']).convert('RGB') as clipped:
                        expected.paste(clipped, tuple(mapping['payload_bbox'][:2]))
                check(expected, 'detail', number, mappings, label, basis)
    ve.need(next(items, None) is None, 'unexpected extra packet region/component/tile')


def verify_delivery(packet_path, batch_index, observation, read_delivered, *, manifest_path=None, expected_sha256=None):
    """Fail closed on absent post-transform bytes, unreadable detail or embed usage.

    A future route adapter supplies genuine observed model-input bytes by item ID;
    metadata alone is insufficient. Identical PNG encoding is intentionally
    required for now. Other encodings/resizes need a separately reviewed adapter
    policy; no receipt generated here approves source interpretation.
    """
    packet = validate_packet(packet_path, manifest_path=manifest_path, expected_sha256=expected_sha256)
    ve._integer(batch_index, 'delivery batch', 0, len(packet['batches']) - 1)
    ve.need(isinstance(observation, dict) and observation.get('packet_sha256') == ve.sha(Path(packet_path).read_bytes()), 'delivery packet pin mismatch')
    ve.need(observation.get('observation_point') == 'model-input-post-transform', 'actual model-input transport observation required')
    ve.need(isinstance(observation.get('route'), str) and observation['route'].strip(), 'observed route required')
    usage = ve._integer(observation.get('embed_usage'), 'observed embed usage', 1)
    budget = ve._integer(observation.get('embed_budget'), 'actual route embed budget', 1)
    assert isinstance(usage, int) and isinstance(budget, int)
    ve.need(usage <= budget, 'delivered embed budget exceeded')
    route_limits = observation.get('route_limits')
    ve.need(isinstance(route_limits, dict) and set(route_limits) == set(packet['limits']), 'observed route limits required')
    assert isinstance(route_limits, dict)
    for key in ('max_edge', 'max_image_bytes', 'max_batch_bytes', 'max_images'):
        ve._integer(route_limits[key], 'actual route ' + key, 1)
    expected = packet['batches'][batch_index]
    observed = observation.get('items')
    ve.need(isinstance(observed, list) and all(isinstance(i, dict) for i in observed)
            and [i.get('id') for i in observed] == expected, 'incomplete/duplicate actual delivery coverage')
    assert isinstance(observed, list)
    ve.need(len(observed) <= route_limits['max_images'], 'actual route image count exceeded')
    indexed = {i['id']: i for i in packet['items']}
    delivered_bytes = 0
    for item in observed:
        intended = indexed[item['id']]
        data = read_delivered(item['id'])
        ve.need(isinstance(data, bytes) and ve.sha(data) == intended['sha256'], 'actual delivery transformed/substituted pixels or encoding')
        dimensions = ve._png(data)
        ve.need(list(dimensions) == intended['intended_dimensions'] == item.get('dimensions'), 'actual delivery dimension mismatch')
        ve.need(max(dimensions) <= route_limits['max_edge'] and len(data) <= route_limits['max_image_bytes'], 'actual route dimensions/image bytes exceeded')
        delivered_bytes += len(data)
        ve.need(item.get('sha256') == ve.sha(data) and item.get('transformations') == [], 'actual transport transformation not approved')
        if intended['role'] == 'detail':
            ve.need(item.get('readable') is True, 'actual delivered detail unreadable/unreviewed')
    ve.need(delivered_bytes <= route_limits['max_batch_bytes'], 'actual route batch bytes exceeded')
    return {'verified': True, 'batch': batch_index, 'packet_sha256': observation['packet_sha256'],
            'note': 'Transport contract checked against supplied observations; adapter must establish observation authenticity. Not source approval.'}
