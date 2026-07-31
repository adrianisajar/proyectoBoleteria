from motores.ticket_service import estado_para_total
from motores.validacion import parse_boletas_detailed, parse_int_filter, parse_money, ticket_number_query
from motores.vendor_service import calc_comision_por_boleta


def test_parse_money():
    assert parse_money("1.500") == 1500
    assert parse_money("$ 70.000") == 70000
    assert parse_money("") == 0
    assert parse_money(None) == 0


def test_parse_boletas_detailed():
    nums, invalid, oor, dups = parse_boletas_detailed("0001, 0002, 0001, abc, 10000")
    assert nums == [1, 2]
    assert invalid == ["abc"]
    assert oor == ["10000"]
    assert dups == [1]


def test_ticket_number_query_exact():
    num, exact = ticket_number_query("0042", [])
    assert num == 42
    assert exact is True


def test_ticket_number_query_partial():
    query, exact = ticket_number_query("42", [])
    assert exact is False
    assert isinstance(query, dict)
    assert "$in" in query
    assert 0 < len(query["$in"]) <= 200


def test_ticket_number_query_no_digits():
    errors = []
    num, exact = ticket_number_query("abc", errors)
    assert num is None
    assert exact is False
    assert errors


def test_parse_int_filter():
    errors = []
    assert parse_int_filter("", "x", errors) is None
    assert parse_int_filter("42", "x", errors, 0, 100) == 42
    parse_int_filter("150", "x", errors, 0, 100)
    assert errors


def test_estado_para_total():
    assert estado_para_total(70000, 70000) == "pagada"
    assert estado_para_total(30000, 70000) == "abonando"
    assert estado_para_total(0, 70000, vendedor_id="LOCAL") == "separada"
    assert estado_para_total(0, 70000, vendedor_id="V1") == "asignada"
    assert estado_para_total(0, 70000) == "disponible"


def test_calc_comision_por_boleta():
    tiers = [{"min": 0, "valor": 0}, {"min": 10, "valor": 10000}, {"min": 21, "valor": 15000}, {"min": 51, "valor": 20000}]
    assert calc_comision_por_boleta(5, tiers) == 0
    assert calc_comision_por_boleta(10, tiers) == 10000
    assert calc_comision_por_boleta(30, tiers) == 15000
    assert calc_comision_por_boleta(60, tiers) == 20000
