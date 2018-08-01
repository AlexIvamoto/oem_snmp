# coding=utf-8

from pysnmp.hlapi import *
from pyzabbix import ZabbixMetric, ZabbixSender
import socket
import re
import os
import json
import time

from event_logger import log_event
from emcli import Emcli


def filter_trap(**kwargs):
    # Фильтр входящих сообщений
    # На вход функции подается некие параметры трапа
    # На основе конфигурационного файла filter.json
    # проверяется, пропускать ли этот трап или нет
    # На настоящий момент проверяются только поля MESSAGE и EVENT_NAME
    # В конфигурационном фале записи состоят из ключа - имени поля, которое проверяется
    # и значения - массива регулярных выражений, которыми это поле проверяется.
    # При совпадении хотя бы с одним из них функция возвращает признак
    # неоходимости фильтрации сообщения
    with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'config',
                           'filter.json'), 'r') as json_file:
        filters = json.load(json_file, encoding='ascii')
        for filter_key, filter_value in filters.iteritems():
            if filter_key in kwargs and kwargs[filter_key] is not None:
                for value in filter_value:
                    regexp = re.compile(value.encode('ascii'))
                    match_object = regexp.search(kwargs[filter_key])
                    if match_object:
                        return True

    return False


def send_trap(environment):
    # Выставляем признак неотправки трапа
    do_not_send_trap = False

    # Загружаем конфиг
    with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'config',
                           'snmp.json'), 'r') as json_file:
        config = json.load(json_file)

    # Маппинг переменных окружения в переменные трапа в соответствии с MIBом
    # # Переменные перечислены в соответсвии с главой
    # # "3.10.2 Passing Event, Incident, Problem Information to an OS Command or Script"
    # # документа Oracle® Enterprise Manager Cloud Control Administrator's Guide
    # # Если зачению переменной трапа может соответствовать несколько переменных окружения
    # # в зависимости от события, которое обрабатывается, такие переменные представлены
    # # в виде словаря, в которых ключ соответствует переменной ISSUE_TYPE - тип события,
    # # значение - переменной, которую нужно подставить
    trap_to_environment_variables = config['trap_to_environment_variables']

    # Все поля трапа OEM, которые мы будем передавать, получены из MIBа omstrap.v1
    trap_parameters = config['trap_parameters']

    # Имя хоста ОЕМ
    hostname = config['hostname']
    zabbix = config['zabbix']

    # На вход получаем параметры окружения в виде словаря, которые создает OMS при вызове скрипта
    # Собираем только те параметры, которые укладываются в стандартный MIB omstrap.v1 Oracle OEM 13c
    # Кроме того, сохраняем в oms_event['oraEMNGEnvironment'] все переменные окружения, мало ли что-то упустили

    oms_event = {'oraEMNGEnvironment': environment,
                 'oraEMNGEventSequenceId': 'null'}

    if 'ISSUE_TYPE' not in environment:
        raise Exception('ISSUE_TYPE not set')

    for trap_variable, os_variable in trap_to_environment_variables.iteritems():
        if type(os_variable) is unicode:
            oms_event.update({trap_variable: environment[os_variable] if os_variable in environment else ''})
        elif type(os_variable) is dict:
            issue_type = environment['ISSUE_TYPE']
            oms_event.update({trap_variable: environment[os_variable[issue_type]] if (issue_type in os_variable and
                                                                                      os_variable[
                                                                                          issue_type] in environment) else ''})

    # Нужно подправить некоторые элементы
    # Во-первых, подрезаем длину сообщения и URL события до 255 символов, чтобы влезало в трап
    oms_event.update({'oraEMNGEventMessage': oms_event['oraEMNGEventMessage'][:255],
                      'oraEMNGEventMessageURL': oms_event['oraEMNGEventMessageURL'][:255],
                      'oraEMNGEventContextAttrs': oms_event['oraEMNGEventContextAttrs'][:255]})

    # Во-вторых, для инцидентов и проблем не передается в переменную SequenceID
    # Будем брать его из SequenceID породившего события
    if oms_event['oraEMNGIssueType'] in ('2', '3'):
        oms_event.update(
            {'oraEMNGEventIssueId': re.search('&issueID=([ABCDEF|0-9]{32})$',
                                              environment['MESSAGE_URL']).group(1)})

        emcli = Emcli()
        event_id = emcli.get_event_id(oms_event['oraEMNGEventIssueId'])
        if event_id is not None and len(event_id) != 0:
            oms_event.update({'oraEMNGEventSequenceId': event_id[0]})
        else:
            oms_event.update({'oraEMNGEventSequenceId': oms_event['oraEMNGEventIssueId']})

        # В-третьих, нужно проверить, есть ли событие с таким же уровнем severity
        # и отправлялось ли по нему сообщение
        # Если есть, трап по инциденту или проблеме отправлять не нужно
        message_sent = emcli.check_message_sent(oms_event['oraEMNGEventIssueId'],
                                                oms_event['oraEMNGEventSeverity'])

        # Подождем 2 секунды, возможно сообщение по событию запаздывает
        if not message_sent:
            time.sleep(2)
            message_sent = emcli.check_message_sent(oms_event['oraEMNGEventIssueId'],
                                                    oms_event['oraEMNGEventSeverity'])

        if message_sent:
            do_not_send_trap = True
            # Если пришел Acknowledged, трап посылаем с ID породившего события
            if oms_event['oraEMNGAssocIncidentAcked'] == 'Yes':
                do_not_send_trap = False

        # Если пришла закрывашка, а само событие закрылось без отправки сообщения,
        # нужно отправить трап, подменив SequenceID на аналогичный параметр события
        if oms_event['oraEMNGEventSeverity'] == 'Clear' and not message_sent:
            do_not_send_trap = False

    # Если не стоит признак не посылать трап,
    if not do_not_send_trap:
        # Проверяем, нужно ли фильтровать трап
        # Если да - отсылать не будем
        if not filter_trap(message=environment['MESSAGE'] if 'MESSAGE' in environment else None,
                           event_name=environment['EVENT_NAME'] if 'EVENT_NAME' in environment else None):
            # Собираем SNMP трап
            # Для этого нужен MIB (Management Information Base)
            # # Есть проблема, Питон не хочет подхватывать напрямую MIB-файл из OMS,
            # # который лежит $OMS_HOME/network/doc/omstrap.v1. Кроме того, в дефолтном файле
            # # слишком много ненужной (устаревшей) информации. Поэтому мы удалили все OIDы oraEM4Alert,
            # # кроме тех которые необходимы для копиляции. После этого скомпилировали полученный MIB
            # # скриптом mibdump.py, который идет в поставке с пакетом pysmi, который ставиться pip'ом
            # # и положил полученный *.py файл в /usr/lib/python2.7/site-packages/pysnmp/smi/mibs с правами 644

            address = socket.gethostbyname(hostname)

            # Собираем переменные трапа
            trap_variables = [(ObjectIdentity('DISMAN-EVENT-MIB', 'sysUpTimeInstance'), TimeTicks(int(time.time()))),
                              (ObjectIdentity('SNMP-COMMUNITY-MIB', 'snmpTrapAddress', 0), address)]

            for trap_variable in trap_parameters:
                trap_variables.append((ObjectIdentity('ORACLE-ENTERPRISE-MANAGER-4-MIB', trap_variable),
                                       oms_event[trap_variable].replace('"', "'") if trap_variable in oms_event else ''))

            # Посылаем трап
            try:
                error_indication, error_status, error_index, var_binds = next(
                    sendNotification(
                        SnmpEngine(),
                        CommunityData('public', mpModel=0),
                        UdpTransportTarget((zabbix['host'], zabbix['port'])),
                        ContextData(),
                        'trap',
                        NotificationType(
                            ObjectIdentity('ORACLE-ENTERPRISE-MANAGER-4-MIB', 'oraEMNGEvent')
                        ).addVarBinds(*trap_variables)
                    )
                )

                if error_indication:
                    oms_event.update({'TrapState': 'exception'})
                    log_event(oms_event_to_log=oms_event)
                    raise Exception(error_indication)
                else:
                    oms_event.update({'TrapState': 'send snmp'})
            except Exception as e:
                log_event(oms_event_to_log=oms_event)
                raise e

            # Собираем Zabbix трап
            # Собираем переменные трапа
            trap_variables = dict()

            for trap_variable in trap_parameters:
                if trap_variable in oms_event:
                    trap_variables.update({trap_variable.encode('ascii').replace('oraEMNG', '').replace('Event', ''):
                                               oms_event[trap_variable].encode('ascii')})

            # Формируем метрику
            try:
                m = ZabbixMetric(oms_event['oraEMNGEventHostName'], 'data',
                                 json.dumps(trap_variables, indent=3, sort_keys=True))
                zbx = ZabbixSender(zabbix['host'])
                # Отправляем
                response = zbx.send([m])

                if response is not None:
                    if response.failed == 1:
                        oms_event.update({'TrapState': oms_event['TrapState'] + ', exception zabbix'})
                        # raise Exception('Zabbix trap not send')
                    elif response.processed == 1:
                        oms_event.update({'TrapState': oms_event['TrapState'] + ', send zabbix'})
            except Exception as e:
                log_event(oms_event_to_log=oms_event)
                raise e
        else:
            oms_event.update({'TrapState': 'filtered'})
    else:
        oms_event.update({'TrapState': 'skipped'})

    log_event(oms_event_to_log=oms_event)

    # Возвращаем полученный SequenceID
    return oms_event['oraEMNGEventSequenceId']
