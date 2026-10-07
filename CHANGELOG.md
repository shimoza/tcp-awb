# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A project session now asks at most one question per reply, always with the default it takes, and parks every
  other open decision in the project's OPEN.md instead of asking in the chat.
- The owner and an architect can see each month how many replies ended with a question with
  `awb report --by questions`.
