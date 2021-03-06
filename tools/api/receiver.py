import ConfigParser
import argparse
import base64
import errno
import json
import os
import shutil
import subprocess
import sys
import urllib2
from Crypto.Hash import SHA, HMAC
from datetime import datetime
from urllib2 import URLError

import pika
import requests

import tools.utility.log as log
from tools.utility import messageFactory
from tools.utility.util import get_curr_dir

LOGGER = log.get_logger('receiver')


# Make a http request on path with json_data
def http_request(path, method, json_data, http_credentials, header,
                 indexing=None, return_code=False):
    """Create HTTP request
        Arguments:
            :param indexing: (str) Whether there need to be added a
             X-YC-Signature.
                This is because of the verification with indexing script
            :param header: (str) to set Content-type and Accept headers to this
             variable
            :param http_credentials: (list) Basic authorization credentials -
             username, password
                respectively
            :param json_data: (dict) Body of the payload send via request 
            :param method: (str) Request method.
            :param path : (str) Path of the request.
            :return a response from the request.
    """
    try:
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        request = urllib2.Request(path, data=json_data)
        request.add_header('Content-Type', header)
        request.add_header('Accept', header)
        if indexing:
            request.add_header('X-YC-Signature', 'sha1={}'.format(indexing))
        base64string = base64.b64encode('%s:%s' % (http_credentials[0], http_credentials[1]))
        request.add_header("Authorization", "Basic %s" % base64string)
        request.get_method = lambda: method
        return opener.open(request)
    except urllib2.HTTPError as e:
        LOGGER.debug('Could not send request with body ' + repr(json_data) + ' and path ' + path)
        if return_code:
            return e
        else:
            raise e
    except URLError as e:
        raise e


def process_sdo(arguments):
    """Processes SDOs. Calls populate script which calls script to parse all the modules
    on the given path which is one of the params. Populate script will also send the
    request to populate confd on given ip and port. It will also copy all the modules to 
    parent directory of this project /api/sdo. It will also call indexing script to
    update searching.
            Arguments:
                :param arguments: (list) list of arguments sent from api sender
                :return (__response_type) one of the response types which is either
                    'Failed' or 'Finished successfully' 
    """
    LOGGER.debug('Processing sdo')
    tree_created = True if arguments[-4] == 'True' else False
    arguments = arguments[:-4]
    direc = '/'.join(arguments[6].split('/')[0:3])
    arguments.append("--result-html-dir")
    arguments.append(result_dir)
    arguments.append('--api-port')
    arguments.append(repr(api_port))
    arguments.append('--api-protocol')
    arguments.append(api_protocol)
    arguments.append('--save-file-dir')
    arguments.append(save_file_dir)

    with open("log.txt", "wr") as f:
        try:
            subprocess.check_call(arguments, stderr=f)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(direc)
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error while parsing or populating data'

    try:
        os.makedirs(get_curr_dir(__file__) + '/../../api/sdo/')
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            return __response_type[0] + '#split#Server error - could not create directory'

    if tree_created:
        subprocess.call(["cp", "-r", direc + "/temp/.", get_curr_dir(__file__) + "/../../api/sdo/"])
        with open('../parseAndPopulate/' + direc + '/prepare.json',
                  'r') as f:
            global all_modules
            all_modules = json.load(f)
        if notify_indexing:
            send_to_indexing(yangcatalog_api_prefix,
                             direc + '/prepare.json', [arguments[11],
                                                       arguments[12]],
                             sdo_type=True, force_indexing=False)

    return __response_type[1]


def create_signature(secret_key, string):
    """ Create the signed message from api_key and string_to_sign
            Arguments:
                :param string: (str) String that needs to be signed
                :param secret_key: Secret key that the string will be signed with
                :return A string of 2* `digest_size` bytes. It contains only
                    hexadecimal ASCII digits.
    """
    string_to_sign = string.encode('utf-8')
    hmac = HMAC.new(secret_key, string_to_sign, SHA)
    return hmac.hexdigest()


def send_to_indexing(yc_api_prefix, modules_to_index, credentials, apiIp = None,
                     sdo_type=False, delete=False, from_api=True, set_key=None,
                     force_indexing=True):
    """ Sends the POST request which will activate indexing script for modules which will
    help to speed up process of searching. It will create a json body of all the modules
    containing module name and path where the module can be found if we are adding new
    modules. Other situation can be if we need to delete module. In this case we are sending
    list of modules that need to be deleted.
            Arguments:
                :param yc_api_prefix: (str) prefix for sending request to api
                :param modules_to_index: (json file) prepare.json file generated while parsing
                    all the modules. This file is used to iterate through all the modules.
                :param credentials: (list) Basic authorization credentials - username, password
                    respectively.
                :param sdo_type: (bool) Whether or not it is sdo that needs to be sent.
                :param delete: (bool) Whether or not we are deleting module.
                :param from_api: (bool) Whether or not api sent the request to index.
                :param set_key: (str) String containing key to confirm that it is receiver that sends data. This is
                    is verified before indexing takes place.
                :param force_indexing: (bool) Whether or not we should force indexing even if module exists in cache.
    """
    global api_ip
    if apiIp is not None:
        api_ip = apiIp
    LOGGER.debug('Sending data for indexing')
    mf = messageFactory.MessageFactory()
    if delete:
        body_to_send = json.dumps({'modules-to-delete': modules_to_index},
                                  indent=4)

        mf.send_removed_yang_files(body_to_send)
        for mod in modules_to_index:
            name, revision_organization = mod.split('@')
            revision, organization = revision_organization.split('/')
            path_to_delete_local = "{}{}@{}.yang".format(save_file_dir, name,
                                                         revision)
            data = {'input': {'dependents': [{'name': name}]}}

            response = requests.post(yc_api_prefix + 'search-filter',
                                     auth=(credentials[0], credentials[1]),
                                     json={'input': data})
            if response.status_code == 201:
                modules = json.loads(response.content)
                for mod in modules:
                    m_name = mod['name']
                    m_rev = mod['revision']
                    m_org = mod['organization']
                    url = ('{}://{}:{}/api/config/catalog/modules/module/'
                           '{},{},{}/dependents/{}'.format(confd_protocol,
                                                           confd_ip,
                                                           confdPort, m_name,
                                                           m_rev, m_org, name))
                    requests.delete(url, auth=(credentials[0], credentials[1]),
                                    headers={'Content-Type': 'application/vnd.yang.data+json'})
            if os.path.exists(path_to_delete_local):
                os.remove(path_to_delete_local)
    else:
        with open(modules_to_index, 'r') as f:
            sdos_json = json.load(f)
        post_body = {}
        if from_api:
            if sdo_type:
                prefix = 'api/sdo/'
            else:
                prefix = 'api/vendor/'

            for module in sdos_json['module']:
                response = http_request('{}search/modules/{},{},{}'
                                        .format(yc_api_prefix,
                                                module['name'],
                                                module['revision'],
                                                module['organization']), 'GET',
                                        '',
                                        credentials,
                                        'application/vnd.yang.data+json',
                                        return_code=True)
                code = response.code
                if force_indexing or (code != 200 and code != 201 and code != 204):
                    if module.get('schema'):
                        path = prefix + module['schema'].split('githubusercontent.com/')[1]
                        path = os.path.abspath(get_curr_dir(__file__) + '/../../' + path)
                    else:
                        path = 'module does not exist'
                    post_body[module['name'] + '@' + module['revision'] + '/' + module['organization']] = path
        else:
            for module in sdos_json['module']:
                response = http_request('{}search/modules/{},{},{}'
                                        .format(yc_api_prefix,
                                                module['name'],
                                                module['revision'],
                                                module['organization']), 'GET',
                                        '',
                                        credentials,
                                        'application/vnd.yang.data+json',
                                        return_code=True)
                code = response.code
                if force_indexing or (
                            code != 200 and code != 201 and code != 204):
                    if module.get('schema'):
                        path = module['schema'].split('master')[1]
                        path = os.path.abspath(get_curr_dir(__file__) + '/../../' + path)
                    else:
                        path = 'module does not exist'
                    post_body[module['name'] + '@' + module['revision'] + '/' + module['organization']] = path
        body_to_send = json.dumps({'modules-to-index': post_body}, indent=4)
        #if len(post_body) > 0:
        #    mf.send_added_new_yang_files(body_to_send)

    try:
        set_key = key
    except NameError:
        pass
    LOGGER.info('Sending data for indexing with body {}'.format(body_to_send))

    try:
        pass
        #http_request('https://' + api_ip + '/yang-search/metadata-update.php',
        #             'POST', body_to_send,
        #             credentials, 'application/json',
        #             indexing=create_signature(set_key, body_to_send))
    except urllib2.HTTPError as e:
        LOGGER.error('could not send data for indexing. Reason: {}'
                     .format(e.msg))
    except URLError as e:
        LOGGER.error('could not send data for indexing. Reason: {}'
                     .format(repr(e.message)))


def process_vendor(arguments):
    """Processes vendors. Calls populate script which calls script to parse all
    the modules that are contained in the given hello message xml file or in
    ietf-yang-module xml file which is one of the params. Populate script will
    also send the request to populate confd on given ip and port. It will also
    copy all the modules to parent directory of this project /api/sdo.
    It will also call indexing script to update searching.
            Arguments:
                :param arguments: (list) list of arguments sent from api sender
                :return (__response_type) one of the response types which is
                 either 'Failed' or 'Finished successfully' 
    """
    LOGGER.debug('Processing vendor')
    tree_created = True if arguments[-5] == 'True' else False
    integrity_file_location = arguments[-4]

    arguments = arguments[:-5]
    direc = '/'.join(arguments[5].split('/')[0:3])
    arguments.append("--result-html-dir")
    arguments.append(result_dir)
    arguments.append('--save-file-dir')
    arguments.append(save_file_dir)

    with open("log.txt", "wr") as f:
        try:
            subprocess.check_call(arguments, stderr=f)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(direc)
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error while parsing or populating data'
    try:
        os.makedirs(get_curr_dir(__file__) + '/../../api/vendor/')
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error - could not create directory'

    subprocess.call(["cp", "-r", direc + "/temp/.", get_curr_dir(__file__) + "/../../api/vendor/"])

    if tree_created:
        with open('../parseAndPopulate/' + direc + '/prepare.json',
                  'r') as f:
            global all_modules
            all_modules = json.load(f)
        if notify_indexing:
            send_to_indexing(yangcatalog_api_prefix,
                             direc + '/prepare.json', [arguments[9],
                                                       arguments[10]])

    integrity_file_name = datetime.utcnow().strftime("%Y-%m-%dT%H:%m:%S.%f")[:-3] + 'Z'

    if integrity_file_location != './':
        shutil.move('./integrity.html', integrity_file_location + 'integrity' + integrity_file_name + '.html')
    return __response_type[1]


def process_vendor_deletion(arguments):
    """Deletes vendors. It calls the delete request to confd to delete all the module
    in vendor branch of the yang-catalog.yang module on given path. If the module was
    added by vendor and it doesn't contain any other implementations it will delete the 
    whole module in modules branch of the yang-catalog.yang module. It will also call
    indexing script to update searching.
            Arguments:
                :param arguments: (list) list of arguments sent from api sender
                :return (__response_type) one of the response types which is either
                    'Failed' or 'Finished successfully' 
    """
    vendor, platform, software_version, software_flavor = arguments[0:4]
    credentials = arguments[7:9]
    path_to_delete = arguments[9]

    response = 'work'
    try:
        with open('./cache/catalog.json', 'r') as catalog:
            vendors_data = json.load(catalog)['yang-catalog:catalog']['vendors']
    except IOError:
        LOGGER.warning('Cache file does not exist')
        # Try to create a cache if not created yet and load data again
        response = make_cache(credentials, response)
        if response != 'work':
            return response
        else:
            try:
                with open('./cache/catalog.json', 'r') as catalog:
                    vendors_data = json.load(catalog)['yang-catalog:catalog']['vendors']
            except:
                LOGGER.error('Unexpected error: {}'.format(sys.exc_info()[0]))
                return __response_type[0] + '#split#' + sys.exc_info()[0]

    modules = set()
    modules_that_succeeded = []
    iterate_in_depth(vendors_data, modules)

    response = requests.delete(path_to_delete, auth=(credentials[0], credentials[1]))
    if response.status_code == 404:
        pass
        #return __response_type[0] + '#split#not found'

    for mod in modules:
        try:
            path = protocol + '://' + confd_ip + ':' + repr(confdPort) + '/api/config/catalog/modules/module/' \
                   + mod
            modules_data = json.loads(http_request(path + '?deep', 'GET', '', credentials,
                                                   'application/vnd.yang.data+json').read())
            implementations = modules_data['yang-catalog:module']['implementations']['implementation']
            count_of_implementations = len(implementations)
            count_deleted = 0
            for imp in implementations:
                imp_key = ''
                if vendor and vendor != imp['vendor']:
                    continue
                else:
                    imp_key += imp['vendor']
                if platform != 'None' and platform != imp['platform']:
                    continue
                else:
                    imp_key += ',' + imp['platform']
                if software_version != 'None' and software_version != imp['software-version']:
                    continue
                else:
                    imp_key += ',' + imp['software-version']
                if software_flavor != 'None' and software_flavor != imp['software-flavor']:
                    continue
                else:
                    imp_key += ',' + imp['software-flavor']

                url = path + '/implementations/implementation/' + imp_key
                response = requests.delete(url, auth=(credentials[0], credentials[1]))

                if response.status_code != 204:
                    LOGGER.error('Couldn\'t delete implementation of module on path {} because of error: {}'
                                 .format(path + '/implementations/implementation/' + imp_key, response.content))
                    continue
                count_deleted += 1

            if (count_deleted == count_of_implementations and
                        count_of_implementations != 0):
                name, revision, organization = mod.split(',')
                if organization == vendor:
                    response = requests.delete(path, auth=(credentials[0], credentials[1]))
                    to_add = '{}@{}/{}'.format(name, revision, organization)
                    modules_that_succeeded.append(to_add)
                    if response.status_code != 204:
                        LOGGER.error('Could not delete module on path {} because of error: {}'.format(path, response.content))
                        continue
        except:
            LOGGER.error('Yang file {} doesn\'t exist although it should exist'.format(mod))
    if notify_indexing:
        send_to_indexing(yangcatalog_api_prefix, modules_that_succeeded,
                         credentials, delete=True)
    return __response_type[1]


def iterate_in_depth(value, modules):
    """Iterates through the branch to get to the level with modules
            Arguments:
                :param value: (dict) data through which we will need to iterate
                :param modules: (set) set that will contain all the modules that
                 need to be deleted
    """
    for key, val in value.iteritems():
        if key == 'protocols':
            continue
        if isinstance(val, list):
            for v in val:
                iterate_in_depth(v, modules)
        if isinstance(val, dict):
            if key == 'modules':
                for mod in val['module']:
                    name = mod['name']
                    revision = mod['revision']
                    organization = mod['organization']
                    modules.add(name + ',' + revision + ',' + organization)
            else:
                iterate_in_depth(val, modules)


def make_cache(credentials, response):
    """After we delete or add modules we need to reload all the modules to the file
    for qucker search. This module is then loaded to the memory.
            Arguments:
                :param response: (str) Contains string 'work' which will be sent back if 
                    everything went through fine
                :param credentials: (list) Basic authorization credentials - username, password
                    respectively
                :return 'work' if everything went through fine otherwise send back the reason why
                    it failed.
    """
    path = yangcatalog_api_prefix + 'load-cache'
    try:
        http_request(path, 'POST', '', credentials, 'application/vnd.yang.data+json').read()
    except:
        e = sys.exc_info()[0]
        LOGGER.error('Could not load json to memory-cache. Error: {}'.format(e))
        return __response_type[0] + '#split#Server error - loading to memory'
    return response


def process_module_deletion(arguments):
    """Deletes module. It calls the delete request to confd to delete module on
    given path. This will delete whole module in modules branch of the
    yang-catalog.yang module. It will also call indexing script to update
    searching.
                Arguments:
                    :param arguments: (list) list of arguments sent from api
                     sender
                    :return (__response_type) one of the response types which
                     is either 'Failed' or 'Finished successfully' 
    """
    credentials = arguments[3:5]
    path_to_delete = arguments[5]

    response = requests.delete(path_to_delete, auth=(credentials[0], credentials[1]))
    if response.status_code != 204:
        LOGGER.error('Couldn\'t delete module on path {}. Error : {}'
                     .format(path_to_delete, response.content))
        return __response_type[0] + '#split#' + response.content
    name, revision, organization = path_to_delete.split('/')[-1].split(',')
    if notify_indexing:
        send_to_indexing(yangcatalog_api_prefix,
                         ['{}@{}/{}'.format(name, revision, organization)],
                         credentials, delete=True)
    return __response_type[1]


def run_ietf():
    with open("log.txt", "wr") as f:
        try:
            arguments = ['python', '../ietfYangDraftPull/draftPullLocal.py']
            subprocess.check_call(arguments, stderr=f)
            arguments = ['python',
                         '../ietfYangDraftPull/openconfigPullLocal.py']
            subprocess.check_call(arguments, stderr=f)
            return __response_type[1]
        except subprocess.CalledProcessError as e:
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0]


def on_request(ch, method, props, body):
    """Function called when something was sent from API sender. This function
    will process all the requests that would take too long to process for API.
    When the processing is done we will sent back the result of the request
    which can be either 'Failed' or 'Finished successfully' with corespondent
    correlation id. If the request 'Failed' it will sent back also a reason why
    it failed.
            Arguments:
                :param body: (str) String of arguments that need to be processed
                separated by '#'.
    """
    LOGGER.info('Received request with body {}'.format(body))
    if body == 'run_ietf':
        final_response = run_ietf()
    else:
        arguments = body.split('#')
        global all_modules
        all_modules = None
        if arguments[-3] == 'DELETE':
            if 'http' in arguments[0]:
                final_response = process_module_deletion(arguments)
                credentials = arguments[3:5]
            else:
                final_response = process_vendor_deletion(arguments)
                credentials = arguments[7:9]
        elif '--sdo' in arguments[2]:
            final_response = process_sdo(arguments)
            credentials = arguments[11:13]
            direc = '/'.join(arguments[6].split('/')[0:3])
            shutil.rmtree(direc)
        else:
            final_response = process_vendor(arguments)
            credentials = arguments[10:12]
            direc = '/'.join(arguments[5].split('/')[0:3])
            shutil.rmtree(direc)
        if final_response.split('#split#')[0] == __response_type[1]:
            final_response = make_cache(credentials, final_response)

            if all_modules:
                prefix = '{}://{}:{}'.format(confd_protocol, confd_ip,
                                             confdPort)
                new_modules = []
                for module in all_modules['module']:
                    LOGGER.info(
                        'Searching semver for {}'.format(module['name']))
                    url = '{}search/name/{}'.format(yangcatalog_api_prefix,
                                                    module['name'])
                    response = requests.get(url, auth=(
                                            credentials[0], credentials[1]),
                                            headers={
                                                'Accept': 'application/json'})
                    if response.status_code == 404:
                        module['derived-semantic-version'] = '1.0.0'
                        new_modules.append(module)
                    else:
                        data = json.loads(response.content)
                        rev = module['revision'].split('-')
                        date = datetime(int(rev[0]), int(rev[1]), int(rev[2]))
                        module_temp = {}
                        module_temp['name'] = module['name']
                        module_temp['revision'] = module['revision']
                        module_temp['organization'] = module['organization']
                        module_temp['compilation'] = module[
                            'compilation-status']
                        module_temp['date'] = date
                        module_temp['schema'] = module['schema']
                        modules = [module_temp]
                        semver_exist = True
                        for mod in data['yang-catalog:modules']['module']:
                            module_temp = {}
                            revision = mod['revision']
                            if revision == module['revision']:
                                continue
                            rev = revision.split('-')
                            module_temp['revision'] = revision
                            module_temp['date'] = datetime(int(rev[0]),
                                                           int(rev[1]),
                                                           int(rev[2]))
                            module_temp['name'] = mod['name']
                            module_temp['organization'] = mod['organization']
                            module_temp['schema'] = mod.get('schema')
                            module_temp['compilation'] = mod[
                                'compilation-status']
                            module_temp['semver'] = mod.get(
                                'derived-semantic-version')
                            if module_temp['semver'] is None:
                                semver_exist = False
                            modules.append(module_temp)

                        if len(modules) == 1:
                            module['derived-semantic-version'] = '1.0.0'
                            new_modules.append(module)
                            continue
                        modules = sorted(modules, key=lambda k: k['date'])
                        if modules[-1]['date'] == date and semver_exist:
                            if modules[-1]['compilation'] != 'passed':
                                versions = modules[-2]['semver'].split('.')
                                ver = int(versions[0])
                                ver += 1
                                upgraded_version = '{}.{}.{}'.format(ver, 0, 0)
                                module[
                                    'derived-semantic-version'] = upgraded_version
                                new_modules.append(module)
                            else:
                                if modules[-2]['compilation'] != 'passed':
                                    versions = modules[-2]['semver'].split('.')
                                    ver = int(versions[0])
                                    ver += 1
                                    upgraded_version = '{}.{}.{}'.format(ver, 0,
                                                                         0)
                                    module[
                                        'derived-semantic-version'] = upgraded_version
                                    new_modules.append(module)
                                    continue
                                else:
                                    schema2 = '{}{}@{}.yang'.format(
                                        save_file_dir,
                                        modules[-2]['name'],
                                        modules[-2]['revision'])
                                    schema1 = '{}{}@{}.yang'.format(
                                        save_file_dir,
                                        modules[-1]['name'],
                                        modules[-1]['revision'])
                                arguments = ['pyang', '-P', get_curr_dir(__file__) + '/../../.', '-p',
                                             get_curr_dir(__file__) + '/../../.',
                                             schema1, '--check-update-from',
                                             schema2]
                                pyang = subprocess.Popen(arguments,
                                                         stdout=subprocess.PIPE,
                                                         stderr=subprocess.PIPE)
                                stdout, stderr = pyang.communicate()
                                if stderr == '':
                                    arguments = ["pyang", '-p', get_curr_dir(__file__) + '/../../.', "-f",
                                                 "tree",
                                                 schema1]
                                    pyang = subprocess.Popen(arguments,
                                                             stdout=subprocess.PIPE,
                                                             stderr=subprocess.PIPE)
                                    stdout, stderr = pyang.communicate()
                                    arguments = ["pyang", "-p", get_curr_dir(__file__) + "/../../.", "-f",
                                                 "tree",
                                                 schema2]
                                    pyang = subprocess.Popen(arguments,
                                                             stdout=subprocess.PIPE,
                                                             stderr=subprocess.PIPE)
                                    stdout2, stderr = pyang.communicate()
                                    if stdout == stdout2:
                                        versions = modules[-2]['semver'].split(
                                            '.')
                                        ver = int(versions[2])
                                        ver += 1
                                        upgraded_version = '{}.{}.{}'.format(
                                            versions[0],
                                            versions[1],
                                            ver)
                                        module[
                                            'derived-semantic-version'] = upgraded_version
                                        new_modules.append(module)
                                        continue
                                    else:
                                        versions = modules[-2]['semver'].split(
                                            '.')
                                        ver = int(versions[1])
                                        ver += 1
                                        upgraded_version = '{}.{}.{}'.format(
                                            versions[0],
                                            ver, 0)
                                        module[
                                            'derived-semantic-version'] = upgraded_version
                                        new_modules.append(module)
                                        continue
                                else:
                                    versions = modules[-2]['semver'].split('.')
                                    ver = int(versions[0])
                                    ver += 1
                                    upgraded_version = '{}.{}.{}'.format(ver, 0,
                                                                         0)
                                    module[
                                        'derived-semantic-version'] = upgraded_version
                                    new_modules.append(module)
                                    continue
                        else:
                            mod = {}
                            mod['name'] = modules[0]['name']
                            mod['revision'] = modules[0]['revision']
                            mod['organization'] = modules[0]['organization']
                            modules[0]['semver'] = '1.0.0'
                            response = requests.get(
                                '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                    protocol, confd_ip, confdPort,
                                    mod['name'], mod['revision'],
                                    mod['organization']),
                                auth=('admin', 'admin'), headers={
                                    'Accept': 'application/vnd.yang.data+json'})
                            response = json.loads(response.content)[
                                'yang-catalog:module']
                            response['derived-semantic-version'] = '1.0.0'
                            new_modules.append(response)

                            for x in range(1, len(modules)):
                                mod = {}
                                mod['name'] = modules[x]['name']
                                mod['revision'] = modules[x]['revision']
                                mod['organization'] = modules[x]['organization']
                                if modules[x]['compilation'] != 'passed':
                                    versions = modules[x - 1]['semver'].split(
                                        '.')
                                    ver = int(versions[0])
                                    ver += 1
                                    upgraded_version = '{}.{}.{}'.format(ver, 0,
                                                                         0)
                                    modules[x]['semver'] = upgraded_version
                                    response = requests.get(
                                        '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                            protocol, confd_ip, confdPort,
                                            mod['name'], mod['revision'],
                                            mod['organization']),
                                        auth=('admin', 'admin'), headers={
                                            'Accept': 'application/vnd.yang.data+json'})
                                    response = json.loads(response.content)[
                                        'yang-catalog:module']
                                    response[
                                        'derived-semantic-version'] = upgraded_version
                                    new_modules.append(response)
                                else:
                                    if modules[x - 1][
                                        'compilation'] != 'passed':
                                        versions = modules[x - 1][
                                            'semver'].split('.')
                                        ver = int(versions[0])
                                        ver += 1
                                        upgraded_version = '{}.{}.{}'.format(
                                            ver, 0, 0)
                                        modules[x]['semver'] = upgraded_version
                                        response = requests.get(
                                            '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                                protocol, confd_ip, confdPort,
                                                mod['name'], mod['revision'],
                                                mod['organization']),
                                            auth=('admin', 'admin'), headers={
                                                'Accept': 'application/vnd.yang.data+json'})
                                        response = json.loads(response.content)[
                                            'yang-catalog:module']
                                        response[
                                            'derived-semantic-version'] = upgraded_version
                                        new_modules.append(response)
                                        continue
                                    else:
                                        schema2 = '{}{}@{}.yang'.format(
                                            save_file_dir,
                                            modules[x]['name'],
                                            modules[x]['revision'])
                                        schema1 = '{}{}@{}.yang'.format(
                                            save_file_dir,
                                            modules[x - 1]['name'],
                                            modules[x - 1]['revision'])
                                    arguments = ['pyang', '-p', get_curr_dir(__file__) + '/../../.', '-P',
                                                 get_curr_dir(__file__) + '/../../.',
                                                 schema2,
                                                 '--check-update-from', schema1]
                                    pyang = subprocess.Popen(arguments,
                                                             stdout=subprocess.PIPE,
                                                             stderr=subprocess.PIPE)
                                    stdout, stderr = pyang.communicate()
                                    if stderr == '':
                                        arguments = ["pyang", '-p', get_curr_dir(__file__) + '/../../.',
                                                     "-f", "tree",
                                                     schema1]
                                        pyang = subprocess.Popen(arguments,
                                                                 stdout=subprocess.PIPE,
                                                                 stderr=subprocess.PIPE)
                                        stdout, stderr = pyang.communicate()
                                        arguments = ["pyang", '-p', get_curr_dir(__file__) + '/../../.',
                                                     "-f", "tree",
                                                     schema2]
                                        pyang = subprocess.Popen(arguments,
                                                                 stdout=subprocess.PIPE,
                                                                 stderr=subprocess.PIPE)
                                        stdout2, stderr = pyang.communicate()
                                        if stdout == stdout2:
                                            versions = modules[x - 1][
                                                'semver'].split('.')
                                            ver = int(versions[2])
                                            ver += 1
                                            upgraded_version = '{}.{}.{}'.format(
                                                versions[0], versions[1], ver)
                                            modules[x][
                                                'semver'] = upgraded_version
                                            response = requests.get(
                                                '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                                    protocol, confd_ip,
                                                    confdPort,
                                                    mod['name'],
                                                    mod['revision'],
                                                    mod['organization']),
                                                auth=('admin', 'admin'),
                                                headers={
                                                    'Accept': 'application/vnd.yang.data+json'})
                                            response = \
                                            json.loads(response.content)[
                                                'yang-catalog:module']
                                            response[
                                                'derived-semantic-version'] = upgraded_version
                                            new_modules.append(response)
                                        else:
                                            versions = modules[x - 1][
                                                'semver'].split('.')
                                            ver = int(versions[1])
                                            ver += 1
                                            upgraded_version = '{}.{}.{}'.format(
                                                versions[0], ver, 0)
                                            modules[x][
                                                'semver'] = upgraded_version
                                            response = requests.get(
                                                '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                                    protocol, confd_ip,
                                                    confdPort,
                                                    mod['name'],
                                                    mod['revision'],
                                                    mod['organization']),
                                                auth=('admin', 'admin'),
                                                headers={
                                                    'Accept': 'application/vnd.yang.data+json'})
                                            response = \
                                            json.loads(response.content)[
                                                'yang-catalog:module']
                                            response[
                                                'derived-semantic-version'] = upgraded_version
                                            new_modules.append(response)
                                    else:
                                        versions = modules[x - 1][
                                            'semver'].split('.')
                                        ver = int(versions[0])
                                        ver += 1
                                        upgraded_version = '{}.{}.{}'.format(
                                            ver, 0, 0)
                                        modules[x]['semver'] = upgraded_version
                                        response = requests.get(
                                            '{}://{}:{}/api/config/catalog/modules/module/{},{},{}'.format(
                                                protocol, confd_ip,
                                                confdPort,
                                                mod['name'], mod['revision'],
                                                mod['organization']),
                                            auth=('admin', 'admin'), headers={
                                                'Accept': 'application/vnd.yang.data+json'})
                                        response = json.loads(response.content)[
                                            'yang-catalog:module']
                                        response[
                                            'derived-semantic-version'] = upgraded_version
                                        new_modules.append(response)

                for mod in all_modules['module']:
                    name = mod['name']
                    revision = mod['revision']
                    new_dependencies = mod['dependencies']
                    for new_dep in new_dependencies:
                        if new_dep.get('revision'):
                            search = {'name': new_dep['name'],
                                      'revision': new_dep['revision']}
                        else:
                            search = {'name': new_dep['name']}
                        response = requests.post(yangcatalog_api_prefix
                                                 + 'search-filter',
                            auth=(credentials[0], credentials[1]),
                            json={'input': search})
                        if response.status_code == 200:
                            mods = \
                            json.loads(response.content)['yang-catalog:modules'][
                                'module']
                            for m in mods:
                                if m.get('dependents') is None:
                                    m['dependents'] = []
                                new = {'name': name,
                                       'revision': revision,
                                       'schema': mod['schema']}
                                if new not in m['dependents']:
                                    m['dependents'].append(new)
                                    new_modules.append(m)

                    response = requests.post(yangcatalog_api_prefix
                                             + 'search-filter',
                        auth=(
                            credentials[0], credentials[1]),
                        json={'input': {'dependencies': [{'name': name}]}})
                    if response.status_code == 200:
                        mods = json.loads(response.content)['yang-catalog:modules'][
                            'module']

                        if mod.get('dependents') is None:
                            mod['dependents'] = []
                        for m in mods:
                            passed = False
                            for dependency in m['dependencies']:
                                if dependency['name'] == name:
                                    passed = True
                                    rev = dependency.get('revision')
                                    if rev:
                                        passed = False
                                        if rev == revision:
                                            passed = True
                            if passed:
                                new = {'name': m['name'],
                                       'revision': m['revision'],
                                       'schema': m['schema']}
                                if new not in mod['dependents']:
                                    mod['dependents'].append(new)
                        if len(mod['dependents']) > 0:
                            new_modules.append(mod)
                mod = len(new_modules) % 1000
                for x in range(0, len(new_modules) / 1000):
                    json_modules_data = json.dumps({'modules': {
                        'module': new_modules[x * 1000: (x * 1000) + 1000]}})
                    if '{"module": []}' not in json_modules_data:
                        http_request(prefix + '/api/config/catalog/modules/',
                                     'PATCH',
                                     json_modules_data, credentials,
                                     'application/vnd.yang.data+json')
                rest = (len(new_modules) / 1000) * 1000
                json_modules_data = json.dumps(
                    {'modules': {'module': new_modules[rest: rest + mod]}})
                if '{"module": []}' not in json_modules_data:
                    http_request(prefix + '/api/config/catalog/modules/', 'PATCH',
                                 json_modules_data, credentials,
                                 'application/vnd.yang.data+json')

    LOGGER.info('Receiver is done with id - {} and message = {}'
                .format(props.correlation_id, str(final_response)))

    f = open('./correlation_ids', 'r')
    lines = f.readlines()
    f.close()
    with open('./correlation_ids', 'w') as f:
        for line in lines:
            if props.correlation_id in line:
                new_line = '{} -- {} - {}\n'.format(datetime.now()
                                                    .ctime(),
                                                    props.correlation_id,
                                                    str(final_response))
                f.write(new_line)
            else:
                f.write(line)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-path', type=str, default='../utility/config.ini',
                        help='Set path to config file')
    LOGGER.info('Starting receiver')
    args = parser.parse_args()
    config_path = os.path.abspath('.') + '/' + args.config_path
    config = ConfigParser.ConfigParser()
    config.read(config_path)
    global confd_ip
    confd_ip = config.get('General-Section', 'confd-ip')
    global confdPort
    confdPort = int(config.get('General-Section', 'confd-port'))
    global protocol
    protocol = config.get('General-Section', 'protocol-api')
    global api_ip
    api_ip = config.get('Receiver-Section', 'api-ip')
    global api_port
    api_port = int(config.get('General-Section', 'api-port'))
    global api_protocol
    api_protocol = config.get('General-Section', 'protocol-api')
    global confd_protocol
    confd_protocol = config.get('General-Section', 'protocol')
    global key
    key = config.get('Receiver-Section', 'key')
    global notify_indexing
    notify_indexing = config.get('Receiver-Section', 'notify-index')
    global result_dir
    result_dir = config.get('Receiver-Section', 'result-html-dir')
    global save_file_dir
    save_file_dir = config.get('Receiver-Section', 'save-file-dir')
    global is_uwsgi
    is_uwsgi = config.get('General-Section', 'uwsgi')
    if notify_indexing == 'True':
        notify_indexing = True
    else:
        notify_indexing = False
    global yangcatalog_api_prefix
    separator = ':'
    suffix = api_port
    if is_uwsgi == 'True':
        separator = '/'
        suffix = 'api'
    yangcatalog_api_prefix = '{}://{}{}{}/'.format(api_protocol, api_ip,
                                                   separator, suffix)
    __response_type = ['Failed', 'Finished successfully']
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host='127.0.0.1', heartbeat_interval=0))
    channel = connection.channel()
    channel.queue_declare(queue='module_queue')

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(on_request, queue='module_queue', no_ack=True)

    LOGGER.info('Awaiting RPC request')
    channel.start_consuming()
