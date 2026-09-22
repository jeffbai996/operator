"""Shared job MCP tools. Credentials come from the runner, never model arguments."""
import json
import os

import operator_workspace as workspace


def _schema(name, description, properties):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties, 'additionalProperties': False}}


TOOLS = [
    _schema('job_file', 'Use chat files by ID. stage returns server_path for reading and a Windows browser_path. upload attaches a file to an observed file-input CSS selector in a target owned by this chat. publish stores a generated file from the run artifact directory; never use arbitrary host files.', {
        'action': {'enum': ['stage', 'upload', 'publish']}, 'file_id': {'type': 'string'},
        'target_id': {'type': 'string'}, 'selector': {'type': 'string'}, 'path': {'type': 'string'}}),
    _schema('job_state', 'Read the current job, observed tool evidence IDs, approvals and chat files. Use persisted context before asking the user to repeat details.', {}),
    _schema('job_update', 'Share meaningful job/checkpoint changes. Do not create a checklist for simple answers. new_job is only for a distinct new objective, not a follow-up.', {
        'goal': {'type': 'string'}, 'new_job': {'type': 'boolean'},
        'constraints': {'type': 'array', 'items': {'type': 'string'}},
        'decisions': {'type': 'array', 'items': {'type': 'string'}},
        'checkpoints': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'step': {'type': 'string'}, 'status': {'enum': ['pending', 'inProgress', 'completed']}}, 'required': ['step', 'status']}}}),
    _schema('job_result', 'Publish a useful result card instead of duplicating prose. Confirmed outcomes need observed evidence IDs from job_state and exact confirmation details; a successful click alone is not confirmation.', {
        'title': {'type': 'string'}, 'summary': {'type': 'string', 'description': 'Markdown result body. Use a compact table for ranked options, date/rate calendars, or comparisons (one option per row, 2-4 short columns). Use bullets for non-tabular findings. Add a brief takeaway; do not compress numbered options into a prose paragraph or duplicate the full chat answer. Tables must use Markdown pipes and a header separator, not space-aligned text or code fences. Reserve fenced blocks for actual code.'},
        'status': {'enum': ['found', 'prepared', 'confirmed']}, 'confirmation': {'type': 'string'},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string'}},
        'file_ids': {'type': 'array', 'items': {'type': 'string'}}}),
    _schema('job_approval', 'Before a purchase, booking or outward message, describe the exact committing action. Only approved status authorizes that proposal. If pending, ask in chat and finish this turn; the user can approve across devices. Changed details require a new request.', {
        'kind': {'enum': ['purchase', 'booking', 'message', 'other']},
        'destination': {'type': 'string'}, 'description': {'type': 'string'},
        'amount': {'type': 'number'}, 'currency': {'type': 'string'}}),
]


def available():
    return bool(os.environ.get('OPERATOR_DEMO') != '1' and os.environ.get('OPERATOR_RUN_ID') and os.environ.get('OPERATOR_RUN_CREDENTIAL'))


def call(name, arguments):
    if not available():
        raise PermissionError('no active Operator job')
    if not isinstance(arguments, dict):
        raise ValueError('tool arguments must be an object')
    rid, secret = os.environ['OPERATOR_RUN_ID'], os.environ['OPERATOR_RUN_CREDENTIAL']
    if name == 'job_file':
        import asyncio
        from pathlib import Path
        import operator_file_bridge as bridge
        current = workspace.run_state(rid, secret)
        cid = current['job']['conversation_id']
        action = arguments.get('action')
        if action == 'stage':
            result = bridge.stage_file(cid, arguments.get('file_id'))
        elif action == 'upload':
            result = asyncio.run(asyncio.wait_for(bridge.upload_to_input(cid, arguments.get('file_id'),
                arguments.get('target_id'), arguments.get('selector')), timeout=20))
        elif action == 'publish':
            base = workspace.root() / 'artifacts' / rid
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            candidate = (base / arguments.get('path', '')).resolve()
            if not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
                raise ValueError('publish a file inside the run artifact directory')
            with candidate.open('rb') as stream:
                result = workspace.put_file(cid, candidate.name, stream, source='generated')
        else:
            raise ValueError('unknown file action')
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
    handler = {'job_update': workspace.update_job, 'job_result': workspace.publish_result,
               'job_approval': workspace.request_approval}.get(name)
    result = workspace.run_state(rid, secret) if name == 'job_state' else handler(rid, secret, arguments) if handler else None
    if result is None:
        raise ValueError('unknown job tool')
    return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
