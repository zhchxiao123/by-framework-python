export function ErrorNotice({ message }: { message: string }) {
  return (
    <div className="max-w-[70%] rounded-2xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700 shadow-sm">
      {message}
    </div>
  );
}
