# Security

warmap opens files other people made and, when you ask it to, serves your
captures to a phone on your network. A flaw in either matters.

If you find one, write to Munzzyy1@proton.me or open a private report under
the Security tab of the repository, with what you did, what happened, and the
version or commit. If a crafted file is involved, attach it or say how to
build one. I will answer within a few days, fix it, and credit you in the
changelog unless you would rather not be named.

Please do not open a public issue for anything that lets a capture file run
code, read files it should not, or lets someone on your network reach past
the phone bridge. Anything else is fine as an issue.

What the phone bridge promises, and what it does not, is written down in the
docstring at the top of `warmap/server.py` and in the README's Privacy
section.
