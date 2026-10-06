import type { User } from "../models";
import { useResource } from "../resource";
import { Empty, PageHeader, ResourceStatus, date } from "../components/Status";
export function Users() {
  const resource = useResource<User[]>("/api/admin/users", 30000);
  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Accounts that have signed in. Access and roles are managed through Google Groups."
      />
      <section className="panel">
        <ResourceStatus {...resource} />
        {resource.data?.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Google identity</th>
                  <th>Last sign-in</th>
                </tr>
              </thead>
              <tbody>
                {resource.data.map((user) => (
                  <tr key={user.subject}>
                    <td>{user.email}</td>
                    <td className="identifier">{user.subject}</td>
                    <td>{date(user.last_login_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !resource.loading && <Empty>No users have signed in.</Empty>
        )}
      </section>
    </>
  );
}
