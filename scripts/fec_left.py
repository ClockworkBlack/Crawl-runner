"""fec-reread: how many FEC lines in the crawl database still lack the FEC's own fields (awaiting the re-read)."""
from gacrawl import config

s = config.make_store(config.load())
print(s.db.execute("SELECT count(*) FROM records WHERE kind='contribution' AND json_extract(meta,'$.filing_source')='FEC' "
                   "AND json_extract(meta,'$.committee') IS NOT NULL AND json_extract(meta,'$.fec_v') IS NULL").fetchone()[0])
