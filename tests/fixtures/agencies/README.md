Saved pieces of the authorities' own pages, for tests/test_agencies_sites.py and tests/test_agencies_export.py.

Read Nov. 19-20, 2026. `jekyll_board.html` and `smma_minutes.html` are the sites' own HTML (the WordPress content of
the page). The others were read as text and links (the build sandbox could not reach the sites directly) and laid out
again as plain HTML: the words, order and addresses are the pages'; the tags are not. The readers read a page as lines
in reading order (gacrawl/agencies.py `page_lines`), so they do not depend on the tags. `gtea_board.html` follows the
page's real Elementor structure (a date on a line, who met on a line, then the three file links). Event pages
(`ev_*.html`) use georgia.gov's real class names and its JSON-LD event data.
