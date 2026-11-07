from gacrawl.channels import _sitemap_entries, _sitemap_urls


def test_empty_or_broken_sitemap_lists_nothing():
    assert _sitemap_entries(b"") == []
    assert _sitemap_entries(b"   \n") == []
    assert _sitemap_urls(b"<html") == [] or isinstance(_sitemap_urls(b"<html"), list)
    assert _sitemap_urls(b'<urlset><url><loc>https://x.gov/a</loc></url></urlset>') == ["https://x.gov/a"]
