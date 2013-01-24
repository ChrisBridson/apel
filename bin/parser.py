#!/usr/bin/env python

#   Copyright (C) 2012 STFC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# APEL parser. This is an universal script which supports following systems:
# - BLAH
# - PBS
# - SGE
# - LSF (5.x, 6.x, 7.x 8.x)

'''
    @author: Konrad Jopek, Will Rogers
'''

import logging.config
import os
import sys
import re
import gzip
import ConfigParser
from optparse import OptionParser

from apel import __version__
from apel.db import ApelDb, ApelDbException
from apel.db.records import ProcessedRecord
from apel.common import calculate_hash, set_up_logging
from apel.common.exceptions import install_exc_handler, default_handler
from apel.parsers.blah import BlahParser
from apel.parsers.lsf import LSFParser
from apel.parsers.sge import SGEParser
from apel.parsers.pbs import PBSParser
from apel.parsers.slurm import SlurmParser

log = None

# How many records should be put/fetched to/from database 
# in single query
BATCH_SIZE = 1000
PARSERS = {
           'PBS': PBSParser,
           'LSF': LSFParser,
           'SGE': SGEParser,
           'SLURM': SlurmParser,
           'blah' : BlahParser
           }

class ParserConfigException(Exception):
    '''
    Exception raised when parser is misconfigured.
    '''
    pass

def find_sub_dirs(dirpath):
    '''
    Given a directory path, return a list of paths of any subdirectories, 
    including the directory itself.
    '''
    alldirs = []
    for root, unused_dirs, unused_files in os.walk(dirpath):
        alldirs.append(root)
    
    return alldirs
    

def parse_file(parser, apel_db, fp):
    '''
    Parses file from blah/batch system
    
    @param parser: parser object of correct type
    @param apel_db: object to access APEL database
    @param fp: file object with log
    @return: number of correctly parsed files from file, 
             total number of lines in file 
    '''
    records = []
    
    # we will save information about errors
    # default behaviour: show the list of errors with information
    # how many times given error was raised
    exceptions = {}
    
    parsed = 0
    failed = 0
    ignored = 0
    
    for i, line in enumerate(fp):
        try:
            record = parser.parse(line)
        except Exception, e:
            log.debug('Error %s on line %d' % (str(e), i))
            failed += 1
            if exceptions.has_key(str(e)):
                exceptions[str(e)] += 1
            else:
                exceptions[str(e)] = 1
        else:
            if record is not None:
                records.append(record)
                parsed += 1
            else:
                ignored += 1
            if len(records) % BATCH_SIZE == 0 and len(records) != 0:
                apel_db.load_records(records)
                del records
                records = []
        
    apel_db.load_records(records)
        
    if parsed == 0:
        log.warn('Failed to parse file.  Is it %s correct?' % str(parser))        
    else:
        log.info('Parsed %d lines' % parsed)
        log.info('Ignored %d lines (incomplete jobs)' % ignored)
        log.info('Failed to parse %d lines' % failed)
        
        for error in exceptions:
            log.error('%s raised %d times' % (error, exceptions[error]))
    
    return parsed, i

        
def scan_dir(parser, dir_location, expr, apel_db, processed):

    updated = []
    try:
        log.info('Directory: %s' % dir_location)
        
        for item in os.listdir(dir_location):
            abs_file = os.path.join(dir_location, item)
            if os.path.isfile(abs_file) and expr.match(item):
                # first, calculate the hash of the file:
                file_hash = calculate_hash(abs_file)
                found = False
                # next, try to find corresponding entry
                # in database
                for pf in processed:
                    if pf.get_field('Hash') == file_hash:
                        # we found corresponding record
                        # we will leave this record unmodified
                        updated.append(pf)
                        found = True
                        log.info('File: %s already parsed, omitting' % abs_file)
                        
                if not found:
                    try:
                        log.info('Parsing file: %s' % abs_file)
                        # try to open as a gzip file, and if it fails try as 
                        # a regular file
                        try:
                            fp = gzip.open(abs_file)
                            parsed, total = parse_file(parser, apel_db, fp)
                        except IOError, e: # not a gzipped file
                            fp = open(abs_file, 'r')
                            parsed, total = parse_file(parser, apel_db, fp)
                            fp.close()
                    except IOError, e:
                        log.error('Cannot open file %s due to: %s' % 
                                     (item, str(e)))
                    except ApelDbException, e:
                        log.error('Failed to parse %s due to a database problem: %s' % (item, e))
                    else:
                        pr = ProcessedRecord()
                        pr.set_field('HostName', parser.machine_name)
                        pr.set_field('Hash',file_hash)
                        pr.set_field('FileName', abs_file)
                        pr.set_field('StopLine', total)
                        pr.set_field('Parsed', parsed)
                        updated.append(pr)
                        
            else:
                log.info('Not a regular file: %s [omitting]' % item)
        
        return updated
    
    except KeyError, e:
        log.fatal('Improperly configured.')
        log.fatal('Check the section for %s , %s' % (str(parser), str(e)))
        sys.exit(1)
    
def handle_parsing(log_type, apel_db, cp):
    '''
    Create the appropriate parser, and scan the configured directory
    for log files, parsing them.
    
    Update the database with the parsed files.
    '''
    log.info('Setting up parser for %s files' % log_type)
    if log_type == 'blah':
        section = 'blah'
    else:
        section = 'batch'
        
    site = cp.get('site_info', 'site_name')
    if site is None or site == '':
        raise ParserConfigException('Site name must be configured.')
        
    machine_name = cp.get('site_info', 'lrms_server')
    if machine_name is None or machine_name == '':
        raise ParserConfigException('LRMS hostname must be configured.')
    
    processed_files = []
    updated_files = []
    # get all processed records from generator
    for record_list in apel_db.get_records(ProcessedRecord):
        processed_files.extend([record for record in record_list if record.get_field('HostName') == machine_name])
        
    root_dir = cp.get(section, 'dir')
    
    try:
        mpi = cp.getboolean(section, 'parallel')
    except ConfigParser.NoOptionError:
        mpi = False
        
    try:
        parser = PARSERS[log_type](site, machine_name, mpi)
    except NotImplementedError, e:
        raise ParserConfigException(e)
    
    if log_type == 'LSF':
        try:
            parser.set_scaling(cp.getboolean('batch', 'scale_host_factor'))
        except ConfigParser.NoOptionError:
            pass
        
    # regular expressions for blah log files and for batch log files
    try:
        prefix = cp.get(section, 'filename_prefix')
        expr = re.compile('^' + prefix + '.*')
    except ConfigParser.NoOptionError:
        try:
            expr = re.compile(cp.get(section, 'filename_pattern'))
        except ConfigParser.NoOptionError:
            log.warning('No pattern specified for %s log file names.' % log_type)
            log.warning('Parser will try to parse all files in directory')
            expr = re.compile('(.*)')
    
    if os.path.isdir(root_dir):
        if cp.getboolean(section, 'subdirs'):
            to_scan = find_sub_dirs(root_dir)
        else:
            to_scan = [root_dir]
        for directory in to_scan:
            updated_files.extend(scan_dir(parser, directory, expr, apel_db, processed_files))
    else:
        log.warn('Directory for %s logs was not set correctly, omitting' % log_type)
    
    apel_db.load_records(updated_files, None)
    log.info('Finished parsing %s log files.' % log_type)
    
    
def main():
    '''
    Parse command line arguments, do initial setup, then initiate 
    parsing process.
    '''
    install_exc_handler(default_handler)
    
    ver = "APEL parser %s.%s.%s" % __version__
    opt_parser = OptionParser(description=__doc__, version=ver)
    opt_parser.add_option("-c", "--config", help="location of config file", 
                          default="/etc/apel/parser.cfg")
    opt_parser.add_option("-l", "--log_config", help="location of logging config file (optional)", 
                          default="/etc/apel/parserlog.cfg")
    options, unused_args = opt_parser.parse_args()
    
    # Read configuration from file 
    try:
        cp = ConfigParser.ConfigParser()
        cp.read(options.config) 
    except Exception, e:
        sys.stderr.write(str(e))
        sys.stderr.write('\n')
        sys.exit(1)
    
    # set up logging
    try:
        if os.path.exists(options.log_config):
            logging.config.fileConfig(options.log_config)
        else:
            set_up_logging(cp.get('logging', 'logfile'), 
                           cp.get('logging', 'level'),
                           cp.getboolean('logging', 'console'))
    except (ConfigParser.Error, ValueError, IOError), err:
        print 'Error configuring logging: %s' % str(err)
        print 'The system will exit.'
        sys.exit(1)

    global log
    log = logging.getLogger('parser')
    log.info('=====================================')
    log.info('Starting apel parser version %s.%s.%s' % __version__)

    # database connection
    try:
        apel_db = ApelDb(cp.get('db', 'backend'),
                         cp.get('db', 'hostname'),
                         cp.getint('db', 'port'),
                         cp.get('db', 'username'),
                         cp.get('db', 'password'),
                         cp.get('db', 'name'))
        apel_db.test_connection()
        log.info('Connection to DB established')
    except KeyError, e:
        log.fatal('Database configured incorrectly.')
        log.fatal('Check the database section for option: %s' % str(e))
        sys.exit(1)
    except Exception, e:
        log.fatal("Database exception: %s" % str(e))
        log.fatal('Parser will exit.')
        log.info('=====================================')
        sys.exit(1)

    # blah parsing 
    try:
        if cp.getboolean('blah', 'enabled'):
            handle_parsing('blah', apel_db, cp)
    except (ParserConfigException, ConfigParser.NoOptionError), e:
        log.fatal('Parser misconfigured: %s' % str(e))    
        log.fatal('Parser will exit.')
        log.info('=====================================')
        sys.exit(1)
    # batch parsing
    try:
        if cp.getboolean('batch', 'enabled'):
            handle_parsing(cp.get('batch', 'type'), apel_db, cp)
    except (ParserConfigException, ConfigParser.NoOptionError), e:
        log.fatal('Parser misconfigured: %s' % str(e))    
        log.fatal('Parser will exit.')
        log.info('=====================================')
        sys.exit(1)
        
    log.info('Parser has completed.')
    log.info('=====================================')
    sys.exit(0)
    
if __name__ == '__main__':
    main()