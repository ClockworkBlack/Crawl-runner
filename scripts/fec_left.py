"""fec-reread: what the re-read still has to do: FEC lines in the crawl database that lack the FEC's own fields
(awaiting the re-read), plus one for each committee whose read stopped part-way (finance.part_read: the re-read
carries those on; Jon Ossoff's Senate committee once stopped in April 2026 with nothing to carry it on)."""
from gacrawl import config
from gacrawl.finance import part_read

s = config.make_store(config.load())
old = s.db.execute("SELECT count(*) FROM records WHERE kind='contribution' AND json_extract(meta,'$.filing_source')='FEC' "
                   "AND json_extract(meta,'$.committee') IS NOT NULL AND json_extract(meta,'$.fec_v') IS NULL").fetchone()[0]
print(old + len(part_read(s)))
