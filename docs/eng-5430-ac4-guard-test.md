# ENG-5430 AC4 guard test — not for merge

This draft PR exists only to prove the Linear Done-guard live: a Done move on a ticket whose attached work PR is
still open must be reverted (DONE-GUARD-REVERTED), and the same move after this PR is closed must stand
(DONE-GUARD-OK). It will be closed unmerged at the end of the test.
