import configparser
"""
This is a helper function to process the gtfs config file (gtfs.conf).
It will ensure all mandatory sections are populated and add default values to
optional configuration items that are not configured with override values.
"""

# Validate the configuration
def validate_config(config_file):
    # Create a ConfigParser object with extended interpolation support
    config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())

    # Read the config file
    config.read(config_file)

    # Dictionary to store default values for optional settings
    optional_defaults = {
        'DEFAULT': {
            'log_level': 'INFO',
            'stop_id': 'None',
            'window_size_hours': '2'
        },
        'Webserver': {
            'uncached': 'False',
            'local_only': 'False',
            'ip_port': '5000',
        }
    }

    # Define mandatory keys and their respective sections (excluding 'DEFAULT' as a normal section)
    mandatory_keys = {
        'API Keys': ['gtfs_regional_static_data', 'gtfs_regional_realtime'],
        'URL': ['gtfs', 'vehicle_positions', 'trip_updates'],
    }

    # Mandatory keys that should be present in the 'DEFAULT' section
    default_mandatory_keys = ['operator']

    # Check mandatory values in the DEFAULT section
    missing_mandatory = []

    for key in default_mandatory_keys:
        if key not in config['DEFAULT']:
            missing_mandatory.append(f"'{key}' in section 'DEFAULT' is missing")

    # Check mandatory values in other sections
    for section, keys in mandatory_keys.items():
        if not config.has_section(section):
            missing_mandatory.append(f"Section '{section}' is missing.")
            continue

        for key in keys:
            if not config.has_option(section, key):
                missing_mandatory.append(f"'{key}' in section '{section}' is missing")

    if missing_mandatory:
        raise ValueError(f"Configuration is missing mandatory keys: {', '.join(missing_mandatory)}")

    # Populate optional values with defaults for each specific section
    for section, defaults in optional_defaults.items():
        if section == 'DEFAULT':
            # Handle defaults in the DEFAULT section
            for key, default_value in defaults.items():
                if key not in config['DEFAULT']:
                    config['DEFAULT'][key] = default_value
        else:
            # Handle defaults in other sections
            if not config.has_section(section):
                config.add_section(section)
            for key, default_value in defaults.items():
                if not config.has_option(section, key):
                    config[section][key] = default_value

    return config

# If we run the module directly we print the parsed arguments.
if __name__ == '__main__':
    # Validate and populate optional defaults
    validated_config = validate_config('gtfs.conf')

    # Print out all sections with values (including DEFAULT values where interpolated)
    for section in validated_config.sections():
        print(f"[{section}]")
        for key, value in validated_config[section].items():
            print(f"{key} = {value}")
        print()

    # Print the values from the DEFAULT section manually
    print("[DEFAULT]")
    for key, value in validated_config['DEFAULT'].items():
        print(f"{key} = {value}")
