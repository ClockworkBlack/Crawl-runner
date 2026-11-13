"""Election history, 1968 to now: Georgia at every level and federal races nationwide, primaries included.

Kept apart from the crawl database on purpose: this lane reads published result files (not web pages of councils),
keeps its own work folder (data/history/) and its own progress file (state/history.json), and hands Electo a bundle
of its own (ga/pipeline_archive/hist/). It can therefore run beside every other job without stopping or waiting for
one, and an interrupted run loses nothing: each file is finished and checkpointed before the next is opened.

    python -m gacrawl.history run [--only SOURCE,..] [--max-minutes N]    read what has not been read yet
    python -m gacrawl.history build --out dist/history                    the bundle Electo's Compare tab reads
    python -m gacrawl.history status                                      what is on file, per source and decade
    python -m gacrawl.history verify --bundle DIR --electo DIR            is the bundle in Electo, file for file

Sources (gacrawl/history/sources.py), each read from an index the publisher keeps, never from a built address:
    medsl          MIT Election Data and Science Lab, Harvard Dataverse: U.S. President (by state), U.S. Senate and
                   U.S. House general elections, every state, 1976 on
    legislators    unitedstates/congress-legislators (public domain): every member of Congress seated since the
                   1968 election: the winners of 1968-1974, and a profile for every member since
    fec            the FEC's "Federal Elections" spreadsheets: primaries, primary runoffs and general elections of
                   every federal candidate, by state
    openelections  OpenElections' Georgia files (transcribed from the Secretary of State's certified results):
                   primaries, runoffs, general and special elections, 2000-2015, by county
    sos_zip        the Secretary of State's "Historical Elections Results" archives, July 2012 to 2015
    sos_pdf        the Secretary of State's results documents, 1988 to 2011 (kept as text for the reader that follows)
    clerk          the Clerk of the House's "Election Statistics", 1968-1974 (kept as text for the reader that follows)
"""
