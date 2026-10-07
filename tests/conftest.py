import os

# Desktop tests use Qt's isolated in-memory clipboard, never the user's desktop.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
