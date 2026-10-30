"""The DB log flush must not invert logging.Handler and buffer locks."""
import subprocess
import sys
import textwrap


def test_database_logging_during_flush_does_not_deadlock():
    program = textwrap.dedent('''
        import logging, threading, os
        import src.web.log_handler as m
        entered = threading.Event()
        class DB:
            def bulk_insert_mappings(self, *args):
                entered.set()
                logging.getLogger('deadlock-probe').error('database error')
            def commit(self): pass
            def close(self): pass
        m.SessionLocal = lambda: DB()
        h = m.DBLogHandler()
        h._timer.cancel()
        logger = logging.getLogger('deadlock-probe')
        logger.handlers = [h]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        h._buffer.append({'message': 'test'})
        locked = threading.Event()
        def emitter():
            h.acquire()
            try:
                locked.set()
                if not entered.wait(3): return
                h.emit(logging.makeLogRecord({'msg':'concurrent', 'levelno':20, 'levelname':'INFO'}))
            finally:
                h.release()
        a = threading.Thread(target=emitter, daemon=True)
        a.start()
        assert locked.wait(3)
        b = threading.Thread(target=h._threaded_flush, daemon=True)
        b.start()
        a.join(3)
        b.join(3)
        os._exit(1 if a.is_alive() or b.is_alive() else 0)
    ''')
    result = subprocess.run([sys.executable, '-c', program], timeout=20, capture_output=True, text=True)
    assert result.returncode == 0, f'log handler deadlock or child error: {result.stderr}'
