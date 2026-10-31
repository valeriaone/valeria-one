"""bk01 (v0.26.0): booking for a team, classes and deposits — who does it, how many seats, money to hold the time

appointments: staff_id (who does it; NULL = the business itself, the one lane every booking shared before — so every
appointment that exists keeps NULL and conflicts with everything exactly as before), service_id, party (people/seats),
the deposit asked (amount, its pay link, until when the time is held, when it was paid, what became of it on a
cancellation) and the no-show fee's link, with the cancellation window as it was when booked. booking_services: capacity
(seats; 1 = as before), party size asked, fixed start times, waitlist, deposit (fixed or a share of the price), no-show /
late-cancel fee, cancellation window. booking_service_staff: who does each service (none = everyone who takes bookings).
staff_members: bookable (off for everyone: nothing changes until the owner says who takes bookings), their own weekly
hours, a colour. staff_time_off: days off and absences. booking_waitlist: people waiting for a seat in a full time.
businesses.deposit_hold_minutes (30): how long a time waits for its deposit once the pay link went out.

Data integrity: nothing existing changes and nothing is deleted — every new column is empty or the default that means
"as before" (party 1, capacity 1, bookable false, no deposit). Not a new tool in the menu (it is the Calendar's): no
"holds data" pin, like agr01.

Revision ID: d5a9c3e7b1f4
Revises: b7e4d1f9a2c6
Create Date: 2026-10-31 19:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd5a9c3e7b1f4'
down_revision: Union[str, Sequence[str], None] = 'b7e4d1f9a2c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('booking_service_staff',
    sa.Column('service_id', sa.UUID(), nullable=False),
    sa.Column('staff_id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_booking_service_staff_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_id'], ['booking_services.id'], name=op.f('fk_booking_service_staff_service_id_booking_services'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_booking_service_staff_staff_id_staff_members'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('service_id', 'staff_id', name=op.f('pk_booking_service_staff'))
    )
    op.create_table('booking_waitlist',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('service_id', sa.UUID(), nullable=False),
    sa.Column('staff_id', sa.UUID(), nullable=True),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('party', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='waiting', nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=True),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('waiting','notify','notified','unreachable','removed')", name=op.f('ck_booking_waitlist_status')),
    sa.CheckConstraint('party BETWEEN 1 AND 1000', name=op.f('ck_booking_waitlist_party')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_booking_waitlist_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_booking_waitlist_contact_id_contacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_id'], ['booking_services.id'], name=op.f('fk_booking_waitlist_service_id_booking_services'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_booking_waitlist_staff_id_staff_members'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_waitlist'))
    )
    op.create_index('ix_booking_waitlist_slot', 'booking_waitlist', ['business_id', 'service_id', 'starts_at', 'created_at'], unique=False)
    op.create_index('uq_booking_waitlist_once', 'booking_waitlist', ['service_id', 'starts_at', 'contact_id'], unique=True, postgresql_where=sa.text("status IN ('waiting', 'notify')"))
    op.create_table('staff_time_off',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('staff_id', sa.UUID(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('ends_at > starts_at', name=op.f('ck_staff_time_off_times')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_staff_time_off_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_staff_time_off_staff_id_staff_members'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_staff_time_off'))
    )
    op.create_index('ix_staff_time_off_staff', 'staff_time_off', ['staff_id', 'starts_at'], unique=False)
    op.add_column('appointments', sa.Column('staff_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('service_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('party', sa.Integer(), server_default=sa.text('1'), nullable=False))
    op.add_column('appointments', sa.Column('deposit_minor', sa.BigInteger(), nullable=True))
    op.add_column('appointments', sa.Column('deposit_payment_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('deposit_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointments', sa.Column('deposit_paid_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointments', sa.Column('deposit_outcome', sa.String(length=8), nullable=True))
    op.add_column('appointments', sa.Column('fee_minor', sa.BigInteger(), nullable=True))
    op.add_column('appointments', sa.Column('fee_payment_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('cancel_hours', sa.Integer(), nullable=True))
    op.create_index('ix_appointments_hold', 'appointments', ['deposit_due_at'], unique=False, postgresql_where=sa.text('deposit_due_at IS NOT NULL'))
    op.create_index('ix_appointments_deposit_pay', 'appointments', ['deposit_payment_id'], unique=False, postgresql_where=sa.text('deposit_payment_id IS NOT NULL'))
    op.create_index('ix_appointments_staff', 'appointments', ['business_id', 'staff_id', 'starts_at'], unique=False, postgresql_where=sa.text('staff_id IS NOT NULL'))
    op.create_foreign_key(op.f('fk_appointments_fee_payment_id_payments'), 'appointments', 'payments', ['fee_payment_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_appointments_staff_id_staff_members'), 'appointments', 'staff_members', ['staff_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_appointments_service_id_booking_services'), 'appointments', 'booking_services', ['service_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_appointments_deposit_payment_id_payments'), 'appointments', 'payments', ['deposit_payment_id'], ['id'], ondelete='SET NULL')
    op.add_column('booking_services', sa.Column('capacity', sa.Integer(), server_default=sa.text('1'), nullable=False))
    op.add_column('booking_services', sa.Column('ask_party', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('booking_services', sa.Column('max_party', sa.Integer(), nullable=True))
    op.add_column('booking_services', sa.Column('starts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('booking_services', sa.Column('waitlist', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('booking_services', sa.Column('deposit_minor', sa.BigInteger(), nullable=True))
    op.add_column('booking_services', sa.Column('deposit_bp', sa.Integer(), nullable=True))
    op.add_column('booking_services', sa.Column('fee_minor', sa.BigInteger(), nullable=True))
    op.add_column('booking_services', sa.Column('cancel_hours', sa.Integer(), nullable=True))
    op.add_column('businesses', sa.Column('deposit_hold_minutes', sa.Integer(), server_default=sa.text('30'), nullable=False))
    op.add_column('staff_members', sa.Column('bookable', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('staff_members', sa.Column('booking_hours', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('staff_members', sa.Column('color', sa.String(length=7), server_default='', nullable=False))

    op.create_check_constraint(op.f('ck_businesses_deposit_hold'), 'businesses', 'deposit_hold_minutes BETWEEN 5 AND 1440')
    op.create_check_constraint(op.f('ck_booking_services_capacity'), 'booking_services', 'capacity BETWEEN 1 AND 1000')
    op.create_check_constraint(op.f('ck_booking_services_max_party'), 'booking_services', 'max_party IS NULL OR max_party BETWEEN 1 AND 1000')
    op.create_check_constraint(op.f('ck_booking_services_deposit'), 'booking_services', 'deposit_minor IS NULL OR deposit_minor > 0')
    op.create_check_constraint(op.f('ck_booking_services_deposit_bp'), 'booking_services', 'deposit_bp IS NULL OR deposit_bp BETWEEN 1 AND 10000')
    op.create_check_constraint(op.f('ck_booking_services_fee'), 'booking_services', 'fee_minor IS NULL OR fee_minor > 0')
    op.create_check_constraint(op.f('ck_booking_services_cancel_hours'), 'booking_services', 'cancel_hours IS NULL OR cancel_hours BETWEEN 1 AND 720')
    op.create_check_constraint(op.f('ck_appointments_party'), 'appointments', 'party BETWEEN 1 AND 1000')
    op.create_check_constraint(op.f('ck_appointments_deposit'), 'appointments', 'deposit_minor IS NULL OR deposit_minor > 0')
    op.create_check_constraint(op.f('ck_appointments_deposit_outcome'), 'appointments',
                               "deposit_outcome IS NULL OR deposit_outcome IN ('kept','refund','late')")


def downgrade() -> None:
    """Downgrade schema."""
    for table, name in (('appointments', 'deposit_outcome'), ('appointments', 'deposit'), ('appointments', 'party'),
                        ('booking_services', 'cancel_hours'), ('booking_services', 'fee'), ('booking_services', 'deposit_bp'),
                        ('booking_services', 'deposit'), ('booking_services', 'max_party'), ('booking_services', 'capacity'),
                        ('businesses', 'deposit_hold')):
        op.drop_constraint(op.f(f'ck_{table}_{name}'), table, type_='check')
    op.drop_column('staff_members', 'color')
    op.drop_column('staff_members', 'booking_hours')
    op.drop_column('staff_members', 'bookable')
    op.drop_column('businesses', 'deposit_hold_minutes')
    op.drop_column('booking_services', 'cancel_hours')
    op.drop_column('booking_services', 'fee_minor')
    op.drop_column('booking_services', 'deposit_bp')
    op.drop_column('booking_services', 'deposit_minor')
    op.drop_column('booking_services', 'waitlist')
    op.drop_column('booking_services', 'starts')
    op.drop_column('booking_services', 'max_party')
    op.drop_column('booking_services', 'ask_party')
    op.drop_column('booking_services', 'capacity')
    op.drop_constraint(op.f('fk_appointments_deposit_payment_id_payments'), 'appointments', type_='foreignkey')
    op.drop_constraint(op.f('fk_appointments_service_id_booking_services'), 'appointments', type_='foreignkey')
    op.drop_constraint(op.f('fk_appointments_staff_id_staff_members'), 'appointments', type_='foreignkey')
    op.drop_constraint(op.f('fk_appointments_fee_payment_id_payments'), 'appointments', type_='foreignkey')
    op.drop_index('ix_appointments_staff', table_name='appointments', postgresql_where=sa.text('staff_id IS NOT NULL'))
    op.drop_index('ix_appointments_deposit_pay', table_name='appointments', postgresql_where=sa.text('deposit_payment_id IS NOT NULL'))
    op.drop_index('ix_appointments_hold', table_name='appointments', postgresql_where=sa.text('deposit_due_at IS NOT NULL'))
    op.drop_column('appointments', 'cancel_hours')
    op.drop_column('appointments', 'fee_payment_id')
    op.drop_column('appointments', 'fee_minor')
    op.drop_column('appointments', 'deposit_outcome')
    op.drop_column('appointments', 'deposit_paid_at')
    op.drop_column('appointments', 'deposit_due_at')
    op.drop_column('appointments', 'deposit_payment_id')
    op.drop_column('appointments', 'deposit_minor')
    op.drop_column('appointments', 'party')
    op.drop_column('appointments', 'service_id')
    op.drop_column('appointments', 'staff_id')
    op.drop_index('ix_staff_time_off_staff', table_name='staff_time_off')
    op.drop_table('staff_time_off')
    op.drop_index('uq_booking_waitlist_once', table_name='booking_waitlist', postgresql_where=sa.text("status IN ('waiting', 'notify')"))
    op.drop_index('ix_booking_waitlist_slot', table_name='booking_waitlist')
    op.drop_table('booking_waitlist')
    op.drop_table('booking_service_staff')
