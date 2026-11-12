from django.db import migrations


def remove_legacy_attempts(apps, schema_editor):
    connection=schema_editor.connection
    table="audit_outboxevent"
    if table not in connection.introspection.table_names():return
    with connection.cursor() as cursor:
        columns={item.name for item in connection.introspection.get_table_description(cursor,table)}
        if "attempts" not in columns or "attempt_count" not in columns:return
        quoted=schema_editor.quote_name(table)
        if connection.vendor=="postgresql":
            cursor.execute(f"UPDATE {quoted} SET attempt_count=GREATEST(COALESCE(attempt_count,0),COALESCE(attempts,0))")
        else:
            cursor.execute(f"UPDATE {quoted} SET attempt_count=MAX(COALESCE(attempt_count,0),COALESCE(attempts,0))")
        cursor.execute(f"ALTER TABLE {quoted} DROP COLUMN attempts")


class Migration(migrations.Migration):
    dependencies=[("audit","0002_alter_outboxevent_available_at_and_more")]
    operations=[migrations.RunPython(remove_legacy_attempts,migrations.RunPython.noop)]
