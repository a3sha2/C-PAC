def main():

    import os
    import argparse

    from CPAC.QPP.prep_QPP import prep_inputs

    parser = argparse.ArgumentParser()
    parser.add_argument("group_config_template",type=str,help="Please provide the path to the group config template containing the QPP options")
    args = parser.add_parse()

    if not args.group_config_template:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT),args.group_config_template)