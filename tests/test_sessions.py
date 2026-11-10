from crows.store import TaskStore


def test_session_messages_persist_and_title_from_first_user_message(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session()
    store.add_session_message(session['id'], 'user', 'Help me debug this code')
    store.add_session_message(session['id'], 'assistant', 'Sure, share the error.')
    reopened = TaskStore(tmp_path / 'sessions.sqlite3')
    saved = reopened.get_session(session['id'])
    assert saved['title'] == 'Help me debug this code'
    messages = reopened.list_session_messages(session['id'])
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert messages[1]['content'] == 'Sure, share the error.'


def test_session_delete_cascades_messages(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session('Temporary')
    store.add_session_message(session['id'], 'user', 'Delete this conversation')
    assert store.delete_session(session['id'])
    assert store.get_session(session['id']) is None
    assert store.list_session_messages(session['id']) == []


def test_session_rejects_invalid_role(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session()
    try:
        store.add_session_message(session['id'], 'tool', 'untrusted')
    except ValueError as exc:
        assert 'role' in str(exc).lower()
    else:
        raise AssertionError('Invalid message role should be rejected')
