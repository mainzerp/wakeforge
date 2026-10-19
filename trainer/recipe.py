"""What a wake word model needs to hold up on the device: the amounts the UI and the guide recommend.

Measured with "Dscharwis" (2026-10-18), the first model that worked well on the device:
25,000 synthetic positives, 8,000 sound-alike clips, two mining rounds. The third scan found only 2 new false
triggers in 33.8 h of speech, so more rounds hardly help; testing on the device tells more.
"""

MIN_POSITIVES = 25_000  # synthetic wake word clips (generate step)
MIN_SOUND_ALIKES = 5_000  # spoken sound-alike clips (generate step)
MINING_ROUNDS = 2  # scan, review, train again; counted as scanned training runs
