def cohort_bucket(flag_name, salt, user_id):
    """Experimental tenant-neutral cohort."""
    return sum(map(ord, user_id)) % 100
