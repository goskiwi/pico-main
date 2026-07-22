def normalize_label(label):
    if not isinstance(label, str):
        raise TypeError("label must be a string")
    return label
