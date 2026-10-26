"""Deadline/financial behavior remains identical as history grows."""
from datetime import datetime, timedelta, timezone

from app.data import DemoStore


class CountedList(list):
    visits = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visits += 1
            yield item


def test_review_deadlines_scan_each_history_once_and_keep_future_reviews_private():
    now = datetime.now(timezone.utc)
    store = DemoStore(seed=False)
    for i in range(1000):
        store.orders.append({'id':str(i), 'status':'completed',
                             'review_deadline':now if i % 2 else now + timedelta(days=1)})
        store.reviews.append({'order_id':str(i), 'published':False})
        store.buyer_reviews.append({'order_id':str(i), 'published':False})
    store.reviews.extend([{'order_id':'orphan','published':False}, {'order_id':'1'}])
    store.reviews = CountedList(store.reviews)
    store.buyer_reviews = CountedList(store.buyer_reviews)
    assert store.process_due_order_events(now) == 1000
    assert store.reviews.visits == 1002 and store.buyer_reviews.visits == 1000
    assert store.reviews[0]['published'] is False
    assert store.reviews[1]['published'] is True
    assert store.reviews[-2]['published'] is False
    assert 'published' not in store.reviews[-1]
    assert store.process_due_order_events(now) == 0


def test_auto_completion_and_room_closure_still_happen_before_review_publication():
    now = datetime.now(timezone.utc)
    store = DemoStore()
    order = store.buy({'id':'buyer','username':'buyer','display_name':'Buyer'}, 'minutes-magic')
    store.deliver(order, 'Delivery')
    order['auto_close_at'] = now
    store.orders = [order]
    store.reviews = [{'order_id':order['id'],'published':False}]
    store.buyer_reviews = []
    assert store.process_due_order_events(now) == 1
    assert order['status'] == 'completed' and order['license_key']
    assert order['review_deadline'] == now + timedelta(days=10)
    assert store.reviews[0]['published'] is False
    assert store.process_due_order_events(now + timedelta(days=10)) == 1
    assert store.reviews[0]['published'] is True
    order.pop('room_closed_at')
    order['room_closes_at'] = now + timedelta(days=11)
    assert store.process_due_order_events(now + timedelta(days=11)) == 1
    assert order['room_closed_at'] == order['room_closes_at']


def test_seller_counts_equal_single_tool_rule_with_one_order_scan():
    store = DemoStore(seed=False)
    statuses = ['in_progress','awaiting_acceptance','cancel_pending','completed','cancelled','pending_payment']
    store.orders = CountedList({'tool_slug':f'tool-{i % 101}', 'status':statuses[i % 6]} for i in range(10000))
    slugs = [f'tool-{i}' for i in range(100)] + ['missing']
    actual = store.tool_active_order_counts(slugs)
    assert store.orders.visits == 10000
    assert actual == {slug:store.tool_active_orders(slug) for slug in slugs}
    assert actual['missing'] == 0
