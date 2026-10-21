"""ISO 20022 CAMT.053 bank-to-customer statements → ``financial_transaction`` events.

Streaming, namespace-agnostic (any ``camt.053.001.xx`` version is read by local
element names; the namespace is recorded). Per ``Stmt``: account, stated
balances (``Bal``: OPBD/PRCD opening, CLBD closing), the transactions summary
and every ``Ntry``. Per entry: ``Amt``/``@Ccy`` as an exact decimal, the
``CdtDbtInd`` sign (the indicator states the booking direction, also for
reversals; ``RvslInd`` is kept), status (``BOOK``/``PDNG``/``INFO``),
``BookgDt``/``ValDt`` as ``Dt`` (day precision, zone unknown) or ``DtTm``
(explicit offset when present), and the ``NtryDtls/TxDtls`` (references,
related parties, remittance information). A batch entry with several
``TxDtls`` keeps them all; counterparty fields are filled only when exactly one
transaction is present.

The balance check (``ledger.bank``) is computed by code; a mismatch is reported,
never corrected. DOCTYPE/ENTITY declarations are refused.
"""
from __future__ import annotations

from pathlib import Path

from ..ledger import bank, money, store, xmlstream
from ..timeinfo import from_iso
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, legacy_instant, record_import_run,
                          stored_path)

PARSER_NAME = 'camt053'
PARSER_VERSION = '1'
STMT_FIELDS = frozenset({'Id', 'ElctrncSeqNb', 'LglSeqNb', 'CreDtTm', 'FrToDt', 'Acct', 'Bal', 'TxsSummry', 'Ntry',
                         'AddtlStmtInf', 'RptgSrc', 'CpyDplctInd'})
_DATE_KINDS = {'BookgDt': ('booking_date', 'created'), 'ValDt': ('value_date', 'other')}
_XML_DECIMAL = money.NumberFormat('.', ())


def _account(node: xmlstream.Node | None) -> dict:
    if node is None:
        return {}
    ident = node.find('Id')
    out = {'iban': ident.text_of('IBAN') if ident else None,
           'other_id': ident.text_of('Othr', 'Id') if ident else None, 'currency': node.text_of('Ccy'),
           'name': node.text_of('Nm'), 'owner': node.text_of('Ownr', 'Nm')}
    return {k: v for k, v in out.items() if v is not None}


def _party(details: xmlstream.Node, role: str) -> dict:
    parties = details.find('RltdPties')
    if parties is None:
        return {}
    party = parties.find(role)
    name = None
    if party is not None:
        name = party.text_of('Nm') or party.text_of('Pty', 'Nm')
    account = _account(parties.find(role + 'Acct'))
    ultimate = parties.find('Ultmt' + role)
    out = {'name': name, 'account': account.get('iban') or account.get('other_id'),
           'ultimate_name': (ultimate.text_of('Nm') or ultimate.text_of('Pty', 'Nm')) if ultimate is not None else None}
    return {k: v for k, v in out.items() if v is not None}


def _date_candidate(node: xmlstream.Node | None, name: str, source: str, problems: list[str]):
    if node is None:
        return None, None
    date_kind, time_kind = _DATE_KINDS.get(name, (name, 'other'))
    text = node.text_of('DtTm') or node.text_of('Dt')
    if text is None:
        problems.append(f'{name}_without_Dt_or_DtTm')
        return None, None
    element = 'DtTm' if node.text_of('DtTm') else 'Dt'
    try:
        candidate = from_iso(time_kind, text, f'{source}:{date_kind}',
                             metadata={'date_kind': date_kind, 'element': f'{name}/{element}'})
        candidate.metadata['zone_status'] = ('offset_stated_by_export' if candidate.tz_status == 'explicit_offset'
                                             else 'export_zone_not_stated')
    except ValueError:
        problems.append(f'{name}_unparsed')
        return None, {'raw': text, 'element': element, 'status': 'unparsed'}
    return candidate, {'raw': text, 'element': element}


def _remittance(details: xmlstream.Node) -> dict:
    info = details.find('RmtInf')
    if info is None:
        return {}
    unstructured = [n.text.strip() for n in info.findall('Ustrd') if n.text.strip()]
    structured = [n.text_of('CdtrRefInf', 'Ref') for n in info.findall('Strd')]
    return {k: v for k, v in {'unstructured': unstructured, 'structured_refs': [s for s in structured if s]}.items() if v}


def _entry(node: xmlstream.Node, statement: dict, index: int, limits: CommLimits) -> dict:
    problems: list[str] = []
    amt = node.find('Amt')
    amount_text = amt.text.strip() if amt is not None else None
    currency_raw = amt.attrib.get('Ccy') if amt is not None else None
    parsed = money.parse_number(amount_text, _XML_DECIMAL)
    indicator = node.text_of('CdtDbtInd')
    exact = None
    if parsed['status'] != 'ok':
        problems.append(f"amount_{parsed['status']}")
    elif parsed.get('sign_notation'):
        problems.append('signed_amount_not_allowed_by_schema')
    elif indicator == 'CRDT':
        exact = parsed['exact']
    elif indicator == 'DBIT':
        exact = money.negate(parsed['exact'])
    else:
        problems.append('credit_debit_indicator_missing_or_unknown')
    currency = money.currency_code(currency_raw)
    code = currency['code'] if currency['status'] == 'ok' else None
    if code is None:
        problems.append('currency_missing_or_invalid')
    minor = money.minor_units(exact, code)
    if exact is not None and code and minor['status'] != 'ok':
        problems.append(minor['status'])
    status = node.text_of('Sts') or node.text_of('Sts', 'Cd') or node.text_of('Sts', 'Prtry')
    reversal = node.text_of('RvslInd')
    source = f"camt053:stmt{statement['index']}"
    candidates, raw_dates = [], {}
    for name in ('BookgDt', 'ValDt'):
        candidate, raw = _date_candidate(node.find(name), name, source, problems)
        if candidate is not None:
            candidates.append(candidate)
        if raw is not None:
            raw_dates[name] = raw
    details_node = node.find('NtryDtls')
    transactions = []
    if details_node is not None:
        for details in details_node.findall('TxDtls'):
            refs = details.find('Refs')
            tx_amount = details.text_of('AmtDtls', 'TxAmt', 'Amt')
            related = details.find('RltdDts')
            tx = {'references': {k: refs.text_of(k) for k in ('MsgId', 'AcctSvcrRef', 'PmtInfId', 'InstrId',
                                                              'EndToEndId', 'TxId', 'MndtId', 'ChqNb')
                                 if refs is not None and refs.text_of(k)},
                  'amount': tx_amount, 'debtor': _party(details, 'Dbtr'), 'creditor': _party(details, 'Cdtr'),
                  'remittance': _remittance(details), 'additional': details.text_of('AddtlTxInf')}
            if related is not None:
                for element in ('AccptncDtTm', 'TxDtTm', 'IntrBkSttlmDt'):
                    text = related.text_of(element)
                    if text:
                        tx.setdefault('related_dates', {})[element] = text
            transactions.append(tx)
    counterparty, description = {}, node.text_of('AddtlNtryInf')
    if len(transactions) == 1:
        tx = transactions[0]
        counterparty = tx['debtor'] if indicator == 'CRDT' else tx['creditor'] if indicator == 'DBIT' else {}
        lines = tx['remittance'].get('unstructured') or []
        description = ' '.join(lines) if lines else (tx['additional'] or description)
        for element in ('AccptncDtTm', 'TxDtTm'):
            text = (tx.get('related_dates') or {}).get(element)
            if text:
                try:
                    candidates.append(from_iso('start', text, f'{source}:transaction_date',
                                               metadata={'date_kind': 'transaction_date',
                                                         'element': f'TxDtls/RltdDts/{element}'}))
                except ValueError:
                    problems.append('transaction_date_unparsed')
                break
    elif len(transactions) > 1:
        problems.append('batch_entry_several_transactions_counterparty_not_assigned')
    domain = node.find('BkTxCd')
    bank_code = None
    if domain is not None:
        bank_code = {'domain': domain.text_of('Domn', 'Cd'), 'family': domain.text_of('Domn', 'Fmly', 'Cd'),
                     'subfamily': domain.text_of('Domn', 'Fmly', 'SubFmlyCd'), 'proprietary': domain.text_of('Prtry', 'Cd')}
        bank_code = {k: v for k, v in bank_code.items() if v}
    position = f"Stmt[{statement['index']}]/Ntry[{index}]"
    flow = None if exact is None or money.Decimal(exact) == 0 else ('debit' if money.is_negative(exact) else 'credit')
    raw, truncated = bound_json(node.as_dict(), limits)
    amount = {'raw': amount_text, 'currency_raw': currency_raw, 'indicator': indicator, 'exact': exact,
              'currency': code, 'minor': minor, 'flow': flow, 'reversal_indicator': reversal,
              'sign_convention': 'holder_signed_negative_is_money_out'}
    content_key = store.sha256_of({'kind': 'camt053', 'dates': {k: v.get('raw') for k, v in raw_dates.items()},
                                   'amount': [exact, code], 'refs': node.text_of('AcctSvcrRef'),
                                   'counterparty': counterparty, 'description': description})
    metadata = {
        'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION}, 'namespace': statement['namespace'],
        'locator': {'statement': statement['index'], 'entry': index, 'xml_line': node.line,
                    'statement_id_text': statement.get('id')},
        'amount': amount, 'status': status, 'booked': status in (None, 'BOOK'), 'dates_raw': raw_dates,
        'entry_reference': node.text_of('NtryRef'), 'account_servicer_reference': node.text_of('AcctSvcrRef'),
        'bank_transaction_code': bank_code, 'transactions': transactions, 'raw_element': raw,
        'raw_truncated_values': truncated, 'subtree_truncated': node.truncated,
        'signal': store.signal('active', 'raw_sample', 'iso20022_camt053',
                               statement['account'].get('iban') or statement['account'].get('other_id')),
        'issues': problems, 'content_key': content_key,
    }
    if counterparty.get('account'):
        metadata['counterparty_account_check'] = store.iban_check(counterparty['account'])
    if status not in (None, 'BOOK'):
        problems.append(f'entry_status_{status}')
    primary = next((c for c in candidates if c.kind == 'start'), None) or \
        next((c for c in candidates if c.kind == 'created'), None)
    entry = {'artifact_id': statement['artifact_id'], 'record_kind': 'financial_transaction', 'parser': PARSER_NAME,
             'parser_version': PARSER_VERSION, 'profile_id': None, 'reading_key': '', 'source_position': position,
             'amount_exact': exact, 'currency': code, 'amount_minor': minor['minor'], 'currency_exponent': minor['exponent'],
             'flow': flow, 'counterparty_name': counterparty.get('name'), 'counterparty_account': counterparty.get('account'),
             'description': description, 'duration_s': None, 'content_key': content_key, 'content_occurrence': 0,
             'metadata_json': {'locator': metadata['locator'], 'status': status, 'amount': amount}}
    event = {'source_id': statement['source_id'], 'artifact_id': statement['artifact_id'],
             'event_type': 'financial_transaction', 'ts_start': legacy_instant(primary), 'ts_end': None,
             'direction': flow, 'contact_label': counterparty.get('name'), 'phone_or_address': counterparty.get('account'),
             'subject': '/'.join(v for v in (bank_code or {}).values()) or None, 'body': description,
             'external_id': f'{PARSER_NAME}:{position}', 'confidence': None, 'metadata': metadata}
    return {'position': position, 'problems': problems, 'candidates': candidates, 'event': event, 'entries': [entry],
            'movement': {'exact': exact, 'currency': code, 'booked': status in (None, 'BOOK')}}


def _balances(nodes: list[xmlstream.Node]) -> tuple[dict | None, dict | None, list[dict], list[str]]:
    stated, problems = [], []
    for node in nodes:
        kind = node.text_of('Tp', 'CdOrPrtry', 'Cd') or node.text_of('Tp', 'CdOrPrtry', 'Prtry')
        amt = node.find('Amt')
        parsed = money.parse_number(amt.text.strip() if amt is not None else None, _XML_DECIMAL)
        currency = money.currency_code(amt.attrib.get('Ccy') if amt is not None else None).get('code')
        date = node.text_of('Dt', 'Dt') or node.text_of('Dt', 'DtTm')
        stated.append(bank.balance(node.text_of('CdtDbtInd'), parsed.get('exact') if parsed['status'] == 'ok' else None,
                                   currency, date=date, kind=kind, raw=amt.text.strip() if amt is not None else None))
    openings = [b for b in stated if b['type'] in ('OPBD', 'PRCD')]
    closings = [b for b in stated if b['type'] == 'CLBD']
    if len({b['type'] for b in openings}) == 2:
        openings = [b for b in openings if b['type'] == 'OPBD']  # OPBD is this statement's own opening
    opening = openings[0] if len(openings) == 1 else None
    closing = closings[0] if len(closings) == 1 else None
    if len(openings) > 1:
        problems.append('several_opening_balances')
    if len(closings) > 1:
        problems.append('several_closing_balances')
    return opening, closing, stated, problems


def import_camt053(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
                   collection_id: int | None = None, label: str | None = None,
                   limits: CommLimits = DEFAULT_LIMITS) -> dict:
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='bank_statement', source_label=label or 'Bank statement (CAMT.053)', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id)
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    counts = {'statement_count': 0, 'entries': 0, 'events': 0, 'balance_mismatches': 0, 'not_booked': 0}
    statements, complete = [], True
    current: dict | None = None
    root = namespace = None
    seen_statement_container = False
    total_entries = 0

    def want(tag, parent):
        return tag == 'GrpHdr' or (parent == 'Stmt' and tag in STMT_FIELDS)

    for item in xmlstream.stream(snapshot, want, markers=frozenset({'Stmt', 'BkToCstmrStmt'})):
        kind = item[0]
        if kind == 'root':
            root, namespace = item[1], item[2]
            if root != 'Document':
                raise ValueError(f'not an ISO 20022 Document (root element {root!r})')
            continue
        if kind == 'start' and item[1] == 'BkToCstmrStmt':
            seen_statement_container = True
            continue
        if kind == 'start' and item[1] == 'Stmt':
            current = {'index': counts['statement_count'], 'fields': {}, 'balances': [], 'records': [], 'account': {},
                       'namespace': namespace, 'artifact_id': artifact_id, 'source_id': source_id}
            counts['statement_count'] += 1
            continue
        if kind == 'end' and item[1] == 'Stmt' and current is not None:
            statements.append(_finish(current, issues, counts))
            current = None
            continue
        if kind != 'node' or current is None:
            continue
        node = item[1]
        if node.tag == 'Ntry':
            if total_entries >= limits.max_records:
                if complete:
                    issues.add('record_limit', limit=limits.max_records)
                complete = False
                continue
            total_entries += 1
            current['records'].append(_entry(node, current, len(current['records']), limits))
        elif node.tag == 'Bal':
            current['balances'].append(node)
        elif node.tag == 'Acct':
            current['account'] = _account(node)
        elif node.tag in ('TxsSummry', 'FrToDt'):
            current['fields'][node.tag] = node
        else:
            current['fields'][node.tag] = node.text.strip()
            if node.tag == 'Id':
                current['id'] = node.text.strip()
    if not seen_statement_container:
        raise ValueError('ISO 20022 document without BkToCstmrStmt (not a CAMT.053 statement; '
                         f'namespace {namespace!r})')
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options={},
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            'namespace': namespace, **counts, 'statements': statements, 'coverage': coverage, 'issues': issues.as_dict()}


def _summary_check(summary: xmlstream.Node | None, records: list[dict]) -> dict | None:
    if summary is None:
        return None
    stated = {'entries': summary.text_of('TtlNtries', 'NbOfNtries'), 'credit_count': summary.text_of('TtlCdtNtries', 'NbOfNtries'),
              'credit_sum': summary.text_of('TtlCdtNtries', 'Sum'), 'debit_count': summary.text_of('TtlDbtNtries', 'NbOfNtries'),
              'debit_sum': summary.text_of('TtlDbtNtries', 'Sum')}
    credits = [r['movement']['exact'] for r in records if r['movement']['exact'] and not money.is_negative(r['movement']['exact'])]
    debits = [money.negate(r['movement']['exact']) for r in records if r['movement']['exact'] and money.is_negative(r['movement']['exact'])]
    computed = {'entries': str(len(records)), 'credit_count': str(len(credits)),
                'credit_sum': money.add_exact(credits) if credits else '0', 'debit_count': str(len(debits)),
                'debit_sum': money.add_exact(debits) if debits else '0'}
    agree = {}
    for key, value in stated.items():
        if value is None:
            continue
        if key.endswith('_sum'):
            agree[key] = money.Decimal(value) == money.Decimal(computed[key])
        else:
            agree[key] = value.strip() == computed[key]
    return {'stated': stated, 'computed': computed, 'agree': agree, 'all_agree': all(agree.values()) if agree else None}


def _finish(current: dict, issues: Issues, counts: dict) -> dict:
    opening, closing, stated, problems = _balances(current['balances'])
    records = current['records']
    status, detail = bank.balance_check(opening, closing, [r['movement'] for r in records])
    occurrences: dict[str, int] = {}
    for record in records:
        key = record['entries'][0]['content_key']
        occurrences[key] = occurrences.get(key, 0) + 1
        record['entries'][0]['content_occurrence'] = occurrences[key]
        record['event']['metadata']['content_occurrence'] = occurrences[key]
        for problem in record['problems']:
            issues.add(problem, position=record['position'])
    summary = _summary_check(current['fields'].get('TxsSummry'), records)
    if status == 'mismatch':
        counts['balance_mismatches'] += 1
        issues.add('balance_mismatch', statement=current['index'], difference=detail['difference'])
    if summary and summary['all_agree'] is False:
        issues.add('transactions_summary_disagrees', statement=current['index'])
    for problem in problems:
        issues.add(problem, statement=current['index'])
    counts['entries'] += len(records)
    counts['events'] += len(records)
    counts['not_booked'] += sum(1 for r in records if not r['movement']['booked'])
    period = current['fields'].get('FrToDt')
    account = current['account']
    row = {'artifact_id': current['artifact_id'], 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
           'reading_key': '', 'statement_key': f"Stmt[{current['index']}]",
           'account_raw': account.get('iban') or account.get('other_id'), 'currency': (opening or closing or {}).get('currency') or account.get('currency'),
           'opening_json': opening or {}, 'closing_json': closing or {}, 'entry_count': len(records),
           'balance_check': status, 'balance_check_json': {**detail, 'summary_check': summary},
           'locator_json': {'statement': current['index'], 'id_text': current.get('id')},
           'metadata_json': {'namespace': current['namespace'], 'account': account, 'balances': stated,
                             'fields': {k: v for k, v in current['fields'].items() if isinstance(v, str)},
                             'period': {'from': period.text_of('FrDtTm') or period.text_of('FrDt'),
                                        'to': period.text_of('ToDtTm') or period.text_of('ToDt')} if period else None,
                             'issues': problems}}
    statement_id = bank.write_statement(row, records)
    return {'statement_id': statement_id, 'index': current['index'], 'id': current.get('id'),
            'account': row['account_raw'], 'entries': len(records), 'balance_check': status,
            'balance': {k: detail.get(k) for k in ('opening', 'sum_of_entries', 'computed_closing', 'stated_closing',
                                                   'difference', 'reason') if k in detail},
            'summary_check': summary}
