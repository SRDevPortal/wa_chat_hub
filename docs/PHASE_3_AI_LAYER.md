# Phase 3 - AI Copilot Layer

## Goal
Provide safe AI assistance for conversation handling, summarization, routing hints, and ERP action drafting.

## Included in app code
- AI service module
- summary generation stub
- reply draft generation stub
- AI suggestion persistence
- AI API endpoints

## Safety posture
- no autonomous clinical advice
- copilot-first design
- suggestions logged in `Chat AI Suggestion`
- future production model integration can replace heuristic logic without changing UI contract
