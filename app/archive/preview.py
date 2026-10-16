"""The same sample and filesystem-aware template preview for every client."""
from app.archive.service import TITLE_SOURCE_JAPANESE, TITLE_SOURCES
from app.conversion.naming import CBZ_SUFFIX, detect_library_limits, render_library_path, validate_library_template
from app.review.models import field_label


LIBRARY_TEMPLATE_SAMPLE = {'category': '同人志', 'artist': '示例作者',
                           'japanese_title': 'サンプル作品', 'english_title': 'Sample Work: Vol.1/2'}


async def template_preview(service, default_root, values):
    template = validate_library_template(str(values.get('library_template') or ''))
    submitted = str(values.get('title_source') or '').strip().lower()
    title_source = submitted if submitted in TITLE_SOURCES else await service.title_source()
    root = await service.library_path() or default_root
    return render_template_preview(template, title_source, detect_library_limits(root))


def render_template_preview(template, title_source, limits):
    """Append the suffix exactly as packing does, preserving dots in titles."""
    sample = LIBRARY_TEMPLATE_SAMPLE
    preferred = sample['japanese_title' if title_source == TITLE_SOURCE_JAPANESE else 'english_title']
    relative = render_library_path(template, {**sample, 'title': preferred}, title_fallback='candidate-1', limits=limits)
    return {'template': template, 'rendered': (relative.parent / f'{relative.name}{CBZ_SUFFIX}').as_posix(),
            'sample': [{'label': field_label(field), 'value': sample[key],
                        **({'note': '当前 {title}' if current else None} if field in {'Title', 'JapaneseTitle'} else {})}
                       for field, key, current in (('Category', 'category', False), ('Artist', 'artist', False),
                           ('JapaneseTitle', 'japanese_title', title_source == TITLE_SOURCE_JAPANESE),
                           ('Title', 'english_title', title_source != TITLE_SOURCE_JAPANESE))]}
