import hashlib


def cohort_bucket(flag_name, salt, user_id):
    payload = f"{flag_name}:{salt}:{user_id}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:8], 16) % 100
