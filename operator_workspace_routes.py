"""Private cockpit workbench routes. The host blueprint supplies its existing guards."""
from flask import jsonify, request, send_file

import operator_workspace as workspace
import operator_diagnostics as diagnostics


def register(bp, *, demo, conversation, control_guard, runner):
    def guarded(mutate=False):
        if demo:
            return None, None, (jsonify(ok=False, error='unavailable in the public demo'), 403)
        data = request.get_json(silent=True) or request.form or request.args
        cid = conversation(data)
        import operator_session
        try:
            operator_session.load(cid)
        except KeyError:
            return cid, data, (jsonify(ok=False, error='chat not found'), 404)
        if mutate:
            error = control_guard(data, cid)
            if error:
                return cid, data, error
        return cid, data, None

    def error_response(exc):
        code = 409 if isinstance(exc, workspace.Conflict) else 404 if isinstance(exc, KeyError) else 400
        return jsonify(ok=False, error=str(exc)), code

    @bp.route('/operator/workspace')
    def operator_workspace_state():
        cid, _, error = guarded()
        if error:
            return error
        import operator_file_bridge
        operator_file_bridge.ensure_running()
        data = workspace.snapshot(cid)
        # The UI needs provenance links/times, never raw tool payloads. Native
        # traces remain in their existing store and model-only job_state.
        for job in data['jobs']:
            job['evidence'] = [{k: e[k] for k in ('id', 'tool', 'observed', 'urls')} for e in job['evidence']]
        data['job'] = data['jobs'][0] if data['jobs'] else None
        response = jsonify(ok=True, **data)
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @bp.route('/operator/workspace/job', methods=['POST'])
    def operator_workspace_job():
        cid, data, error = guarded(True)
        if error:
            return error
        try:
            return jsonify(ok=True, job=workspace.new_job(cid, data.get('goal', '')))
        except (ValueError, KeyError) as exc:
            return error_response(exc)

    @bp.route('/operator/workspace/approval', methods=['POST'])
    def operator_workspace_approval():
        cid, data, error = guarded(True)
        if error:
            return error
        if not isinstance(data.get('approved'), bool):
            return jsonify(ok=False, error='approved must be true or false'), 400
        try:
            result = workspace.decide_approval(cid, data.get('id'), data.get('fingerprint'), data['approved'])
            return jsonify(ok=True, approval=result)
        except (ValueError, KeyError) as exc:
            return error_response(exc)

    @bp.route('/operator/workspace/files', methods=['GET', 'POST'])
    def operator_workspace_files():
        cid, _, error = guarded(request.method == 'POST')
        if error:
            return error
        if request.method == 'GET':
            data = workspace.snapshot(cid)
            return jsonify(ok=True, files=data['files'], storage=data['storage'])
        uploaded = request.files.get('file')
        if not uploaded or not uploaded.filename:
            return jsonify(ok=False, error='choose a file'), 400
        if request.content_length and request.content_length > workspace.limit('FILE', 100) + 65536:
            return jsonify(ok=False, error='file too large'), 413
        try:
            return jsonify(ok=True, file=workspace.put_file(cid, uploaded.filename, uploaded.stream))
        except ValueError as exc:
            return error_response(exc)

    @bp.route('/operator/workspace/files/<fid>', methods=['GET', 'DELETE'])
    def operator_workspace_file(fid):
        cid, _, error = guarded(request.method == 'DELETE')
        if error:
            return error
        try:
            if request.method == 'DELETE':
                if runner().is_running(conversation_id=cid):
                    return jsonify(ok=False, error='stop this run before deleting its files'), 409
                workspace.delete_file(cid, fid)
                return jsonify(ok=True)
            path, asset = workspace.file_path(cid, fid)
            response = send_file(path, as_attachment=True, download_name=asset['name'])
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        except (ValueError, KeyError) as exc:
            return error_response(exc)

    @bp.route('/operator/diagnostics', methods=['GET', 'POST'])
    def operator_diagnostics():
        cid, data, error = guarded(request.method == 'POST')
        if error:
            return error
        if request.method == 'POST':
            diagnostics.debug_recording(data.get('enabled') is True)
        run_id = str(data.get('run_id') or '')
        if run_id:
            with workspace.database() as db:
                if not db.execute('SELECT 1 FROM runs WHERE id=? AND conversation_id=?', (run_id, cid)).fetchone():
                    return jsonify(ok=False, error='run not found in this chat'), 404
        return jsonify(ok=True, **diagnostics.snapshot(run_id))
