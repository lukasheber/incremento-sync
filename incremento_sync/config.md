**enabled** — `true` loads the sync engine into the Incremento add-on and adds *Incremento → Sync Incremento Data…*.
`false` (or removing this add-on) restores Incremento exactly as it was; the sync data already on disk is kept, so turning it
back on resumes where it stopped. *Remove sync data* in the sync window deletes it.

**check_for_updates** — `true` asks GitHub at most once a day whether a newer Incremento Sync is out, and offers its
download page; a version you decline is not offered again. Nothing about you is sent beyond what any web request carries.
`false` turns it off.
